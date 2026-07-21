# bench/measure.py
from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET

from bench.contract import probe_testbed
from bench.gold import junit_ids_to_paths
from bench.languages import get_language
from bench.schema import HarvestedEnv, MeasureRow

# Harvest statuses whose env has a rebuildable artifact worth building. Everything else
# short-circuits before docker.build (design §2.2/§2.5).
_MEASURABLE = ("ok", "legacy_ok", "produced")

_COLLECT_ERR = re.compile(r"((?:[A-Za-z_][\w.]*)?(?:Error|Exception|Warning)):")


def _node_id(tc: ET.Element) -> str:
    cls = tc.get("classname") or ""
    name = tc.get("name") or ""
    return f"{cls}::{name}" if cls else name


def parse_junit(xml_text: str) -> dict:
    # RAT PARITY (RunAnyThing libkit/tools/run_pytest.py:218-221): total/failed/errors/skipped are
    # read from the <testsuite> ATTRIBUTES (pytest's internal report counters); `passed` is counted
    # from <testcase> ELEMENTS (run_pytest.py:275) — the same mixed-unit RAT uses for pass_rate.
    # Node-id lists always come from <testcase> ELEMENTS (needed for display + gold intersection).
    passed_ids, failed_ids, error_ids = [], [], []
    total = failed = errors = skipped = 0
    try:
        root = ET.fromstring(xml_text) if xml_text.strip() else None
    except ET.ParseError:
        root = None
    if root is not None:
        for ts in root.iter("testsuite"):
            total += int(ts.get("tests", 0) or 0)
            failed += int(ts.get("failures", 0) or 0)
            errors += int(ts.get("errors", 0) or 0)
            skipped += int(ts.get("skipped", 0) or 0)
        for tc in root.iter("testcase"):
            nid = _node_id(tc)
            if tc.find("failure") is not None:
                failed_ids.append(nid)
            elif tc.find("error") is not None:
                error_ids.append(nid)
            elif tc.find("skipped") is not None:
                pass
            else:
                passed_ids.append(nid)
    # ELEMENT-CONSISTENT ALTERNATIVE (kept for reference — more correct on subtest suites, where the
    # <testsuite tests> attribute counts subtest REPORTS, not nodes: e.g. Archipelago attr 236474 vs
    # 4315 <testcase> elements -> pass_rate 0.017 attr vs 0.990 element). To switch back, count from
    # elements instead: track a `skipped_ids` list above and return
    #   total   = len(passed_ids) + len(failed_ids) + len(error_ids) + len(skipped_ids)
    #   failed  = len(failed_ids); errors = len(error_ids); skipped = len(skipped_ids)
    return {
        "total": total, "passed": len(passed_ids), "failed": failed, "errors": errors, "skipped": skipped,
        "passed_node_ids": tuple(passed_ids), "failed_node_ids": tuple(failed_ids),
        "error_node_ids": tuple(error_ids),
    }


def parse_collected_node_ids(stdout: str) -> tuple:
    return tuple(ln.strip() for ln in (stdout or "").splitlines() if "::" in ln)


def parse_collect(rc: int, stdout: str) -> dict:
    errs = []
    for ln in (stdout or "").splitlines():
        if _COLLECT_ERR.search(ln) and "::" not in ln:
            errs.append(ln.strip()[:200])
    # EBSR gate — EXACTLY Repo2Run (build_agent/tools/runtest.py:64-72): `pytest --collect-only`
    # succeeds on exit code 0 (collected) OR 5 (no tests collected -> runtest prints "successfully
    # configured" and sys.exit(5)). Any other rc = fail.
    # (STRICTER ALTERNATIVE, if you want rc 5 to fail because all our repos have tests: `rc == 0`.)
    return {"collect_clean": rc in (0, 5), "collect_errors": tuple(errs)}


W = "/testbed"


def _sh(cmd: str) -> list:
    return ["bash", "-lc", cmd]


def measure(env: HarvestedEnv, *, docker, build_timeout: int = 3600, test_timeout: int = 1800) -> MeasureRow:
    agent, repo, m = env.agent, env.repo.full_name, env.meta
    lang = get_language(env.repo.language)
    slug = f"{agent}-{repo}".lower().replace("/", "-")
    base_row = dict(agent=agent, repo=repo, env_status=env.status,
                    tokens_in=m.get("tokens_in"), tokens_out=m.get("tokens_out"),
                    llm_calls=m.get("llm_calls"), turns_used=m.get("turns_used"),
                    produce_s=m.get("produce_s"), meta=dict(m))

    if env.status not in _MEASURABLE or not env.dockerfile:
        sc = env.status if env.status in ("unmeasurable", "error", "legacy_missing") else "missing"
        return MeasureRow(build_ok=False, executed=False, ebsr=False, status=sc, **base_row)

    tag = name = f"bench-{slug}"
    ctx = tempfile.mkdtemp(prefix="benchctx-")
    with open(os.path.join(ctx, "Dockerfile"), "w") as f:
        f.write(env.dockerfile)
    for fname, content in (env.setup_scripts or {}).items():
        with open(os.path.join(ctx, fname), "w") as f:
            f.write(content)

    t0 = time.time()
    build_rc, build_log = docker.build(tag, ctx, timeout=build_timeout)
    build_s = round(time.time() - t0, 2)
    shutil.rmtree(ctx, ignore_errors=True)
    if build_rc != 0:
        return MeasureRow(build_ok=False, build_log_tail=build_log[-2000:], build_s=build_s,
                          executed=False, ebsr=False, status="build_fail", **base_row)

    img_mb = docker.image_size_mb(tag)
    base_mb = docker.image_size_mb(env.base_image) if env.base_image else None
    delta_mb = round(img_mb - base_mb, 2) if (img_mb is not None and base_mb is not None) else None

    timed_out = False
    test_s = None
    junit_xml = ""
    pkgs_out = ""
    try:
        docker.run_detached(tag, name, W)
        docker.exec(name, _sh(f"mkdir -p {W}/logs"))
        docker.exec(name, _sh(lang.ensure_cmd(W)))
        probe = probe_testbed(docker, name, W)
        py_test_files = probe["py_test_files"]
        conforming = probe["status"] == "conforming"

        # EBSR gate (per-language). collect_clean uses the language predicate; parse_collect still
        # supplies the diagnostic collect_errors from the gate stdout.
        grc, gout, _ = docker.exec(name, _sh(lang.gate_cmd(W)))
        collect = parse_collect(grc, gout)
        collect_clean = lang.gate_pass(grc)

        # Node-id collection (Python only; "" => skip). Feeds collected_node_ids + collect_error_count.
        collect_cmd = lang.collect_cmd(W)
        if collect_cmd:
            _, cout2, _ = docker.exec(name, _sh(collect_cmd))
            collected = parse_collected_node_ids(cout2)
            n_collect_errors = cout2.count("ERROR collecting")
        else:
            collected, n_collect_errors = (), 0

        if not conforming:
            return MeasureRow(
                build_ok=True, build_log_tail=build_log[-2000:], build_s=build_s,
                collect_rc=grc, collect_clean=collect_clean,
                collect_errors=collect["collect_errors"], collect_error_count=n_collect_errors,
                collected_node_ids=collected, executed=False, ebsr=False,
                status=probe["status"], py_test_files=py_test_files,
                image_size_mb=img_mb, image_delta_mb=delta_mb, **base_row)

        # Compiled-language gate short-circuits: a failed build cannot run tests, so skip the
        # expensive run and record gate_fail. (Python: short_circuit_gate=False -> run regardless.)
        if lang.short_circuit_gate and not collect_clean:
            return MeasureRow(
                build_ok=True, build_log_tail=build_log[-2000:], build_s=build_s,
                collect_rc=grc, collect_clean=collect_clean,
                collect_errors=collect["collect_errors"], collect_error_count=n_collect_errors,
                collected_node_ids=collected, executed=False, ebsr=False,
                status="gate_fail", py_test_files=py_test_files,
                image_size_mb=img_mb, image_delta_mb=delta_mb, **base_row)

        junit_out = f"{W}/logs/junit.xml"
        run = lang.run_cmd(W, junit_out)
        t1 = time.time()
        _, _, timed_out = docker.exec(name, _sh(run), timeout=test_timeout)
        test_s = round(time.time() - t1, 2)
        _, junit_xml, _ = docker.exec(name, _sh(f"cat {lang.junit_glob(W)} 2>/dev/null || true"))
        pkg_cmd = lang.pkg_count_cmd(W)
        if pkg_cmd:
            _, pkgs_out, _ = docker.exec(name, _sh(pkg_cmd))
    finally:
        docker.rm(name, tag)

    j = parse_junit(junit_xml)
    executed = bool(junit_xml.strip()) and (j["total"] > 0 or "testsuite" in junit_xml)
    if timed_out:
        status = "timed_out"
    elif not collect_clean:
        status = "collect_error"
    elif executed:
        status = "executed"
    else:
        status = "no_tests_collected"
    eff = max(j["total"] - j["skipped"], 0)
    pass_rate = round(j["passed"] / eff, 4) if eff > 0 else 0.0
    try:
        pkg_count = int(pkgs_out.strip().split()[0])
    except (ValueError, IndexError):
        pkg_count = None

    passed_ids = junit_ids_to_paths(j["passed_node_ids"], collected) if collected else j["passed_node_ids"]
    failed_ids = junit_ids_to_paths(j["failed_node_ids"], collected) if collected else j["failed_node_ids"]
    error_ids = junit_ids_to_paths(j["error_node_ids"], collected) if collected else j["error_node_ids"]

    return MeasureRow(
        build_ok=True, build_log_tail=build_log[-2000:], build_s=build_s, test_s=test_s,
        collect_rc=grc, collect_clean=collect_clean, collect_errors=collect["collect_errors"],
        collect_error_count=n_collect_errors, collected_node_ids=collected, executed=executed,
        total=j["total"], passed=j["passed"], failed=j["failed"], errors=j["errors"], skipped=j["skipped"],
        passed_node_ids=passed_ids, failed_node_ids=failed_ids,
        error_node_ids=error_ids, ebsr=executed, pass_rate=pass_rate, timed_out=timed_out,
        status=status, py_test_files=py_test_files,
        image_size_mb=img_mb, image_delta_mb=delta_mb, installed_pkg_count=pkg_count, **base_row)
