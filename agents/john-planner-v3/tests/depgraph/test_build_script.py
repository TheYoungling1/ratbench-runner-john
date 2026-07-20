import re

from python_deps.depgraph.schema import (
    DepGraph, Node, Edge, NodeType, Layer, State, DiscoveredBy, EdgeType,
)
from python_deps.depgraph.build_script import render_build_script, _LAYER_ORDER
from python_deps.depgraph.block import Block, compile_replay_blocks
from python_deps.depgraph.emit import _is_reciped, _apt_name, _pip_spec


def test_empty_graph_emits_preamble():
    out = render_build_script(DepGraph())
    lines = out.splitlines()
    assert lines[0] == "#!/usr/bin/env bash"
    assert "# setup.sh — COMPILED from the certified dependency graph. DO NOT EDIT." in lines
    assert "set -Eeuo pipefail" in lines
    assert out.endswith("\n")


def _pkg(id_, name, version, layer=Layer.PIP, **kw):
    return Node(id=id_, type=NodeType.PACKAGE, name=name, layer=layer,
                discovered_by=DiscoveredBy.RESOLVER, state=State.MISSING,
                version=version, **kw)


def _apt(id_, name, fix, type_=NodeType.SYSTEM_LIB, layer=Layer.SYSTEM, **kw):
    return Node(id=id_, type=type_, name=name, layer=layer,
                discovered_by=DiscoveredBy.PROBE, state=State.MISSING,
                chosen_fix=fix, **kw)


def _project(name="myproj", installable=True):
    return Node(id=f"project:{name}", type=NodeType.PROJECT, name=name,
                layer=Layer.PIP, discovered_by=DiscoveredBy.STATIC_SCAN,
                state=State.UNKNOWN, data={"installable": installable})


_EDITABLE = "python3 -m pip install --break-system-packages --no-deps -e ."


def test_installable_project_editable_install_emitted_last():
    g = DepGraph(nodes=(
        _apt("syslib:libpq-dev", "libpq-dev", "apt:libpq-dev"),
        _pkg("pkg:psycopg2", "psycopg2", "2.9.9"),
        _project(),
    ))
    g = g.with_edge(Edge(src="pkg:psycopg2", dst="syslib:libpq-dev",
                         relation=EdgeType.REQUIRES))
    out = render_build_script(g)
    # emitted exactly once, as the LAST install command (after every apt + pip line)
    assert out.count(_EDITABLE) == 1
    assert out.index(_EDITABLE) > out.index("psycopg2==2.9.9")
    assert out.index(_EDITABLE) > out.index("libpq-dev\n")
    # under its own PROJECT section header, and annotated so render_fidelity sees it
    assert "# ==================== PROJECT" in out
    assert out.index("# ==================== PROJECT") < out.index(_EDITABLE)
    assert "#@node project:myproj" in out


def test_non_installable_project_not_emitted():
    g = DepGraph(nodes=(
        _pkg("pkg:requests", "requests", "2.31.0"),
        _project(installable=False),
    ))
    out = render_build_script(g)
    assert _EDITABLE not in out
    assert "#@node project:myproj" not in out
    assert "# ==================== PROJECT" not in out


def test_deterministic_core_sections_and_commands():
    g = DepGraph(nodes=(
        _apt("syslib:libpq-dev", "libpq-dev", "apt:libpq-dev"),
        _pkg("pkg:psycopg2", "psycopg2", "2.9.9", evidence="ev:import:psycopg2"),
    ))
    g = g.with_edge(Edge(src="pkg:psycopg2", dst="syslib:libpq-dev",
                         relation=EdgeType.REQUIRES))
    out = render_build_script(g)

    # one hoisted apt-get update, exactly once
    assert out.count("apt-get update") == 1
    # section headers present and SYSTEM precedes PIP
    assert (out.index("# ==================== SYSTEM ====================")
            < out.index("# ==================== PIP ===================="))
    # the real commands
    assert "apt-get install -y --no-install-recommends libpq-dev" in out
    assert ("python3 -m pip install --break-system-packages --no-deps "
            "psycopg2==2.9.9") in out
    # annotation provenance is present
    assert "#@node pkg:psycopg2  version=2.9.9  requires=syslib:libpq-dev" in out
    assert "evidence=ev:import:psycopg2" in out
    assert "provider=apt:libpq-dev" in out
    # libpq-dev install line appears before psycopg2 install line (topo)
    assert out.index("libpq-dev\n") < out.index("psycopg2==2.9.9")


def test_apt_update_hoisted_iff_system_nodes_present():
    # POSITIVE: emitted for a system node (fails against the Task 1 stub -> real RED)
    g_sys = DepGraph(nodes=(_apt("syslib:libpq-dev", "libpq-dev", "apt:libpq-dev"),))
    assert "apt-get update" in render_build_script(g_sys)
    # NEGATIVE: not emitted for a pip-only graph
    g_pip = DepGraph(nodes=(_pkg("pkg:requests", "requests", "2.31.0"),))
    assert "apt-get update" not in render_build_script(g_pip)


def test_apt_update_hoisted_once_for_multiple_system_nodes():
    g = DepGraph(nodes=(
        _apt("syslib:libpq-dev", "libpq-dev", "apt:libpq-dev"),
        _apt("syslib:build-essential", "build-essential", "apt:build-essential"),
    ))
    out = render_build_script(g)
    assert out.count("apt-get update") == 1
    assert out.count("export DEBIAN_FRONTEND=noninteractive") == 1
    update_pos = out.index("apt-get update")
    assert update_pos < out.index("libpq-dev\n")
    assert update_pos < out.index("build-essential\n")


def test_node_check_command_emitted_between_annotation_and_install():
    g = DepGraph(nodes=(
        _pkg("pkg:psycopg2", "psycopg2", "2.9.9",
             check_command="python -m pip show psycopg2"),
    ))
    out = render_build_script(g)
    node_idx = out.index("#@node pkg:psycopg2")
    check_idx = out.index("#@check python -m pip show psycopg2")
    install_idx = out.index("psycopg2==2.9.9")
    assert node_idx < check_idx < install_idx


def _need(id_, type_, name, layer, **kw):
    return Node(id=id_, type=type_, name=name, layer=layer,
                discovered_by=DiscoveredBy.STATIC_SCAN, state=State.MISSING, **kw)


def test_need_stubs_are_comment_only():
    g = DepGraph(nodes=(
        _pkg("pkg:psycopg2", "psycopg2", "2.9.9"),
        _need("service:postgres", NodeType.SERVICE, "postgres", Layer.SERVICES,
              check_command="pg_isready -q", evidence="ev:readme:db"),
        _need("config:DATABASE_URL", NodeType.CONFIG, "DATABASE_URL", Layer.CONFIG,
              evidence="ev:settings:DATABASE_URL"),
    ))
    out = render_build_script(g)
    assert "#@need service:postgres  state=missing" in out
    assert "#@check pg_isready -q" in out
    assert "#@need config:DATABASE_URL  state=missing" in out
    # services/config render AFTER pip (highest layer rank)
    assert out.index("psycopg2==2.9.9") < out.index("#@need service:postgres")
    # the stub carries NO real command. SERVICES is the last layer, so the
    # service stub runs to EOF; every non-blank line there must be a comment.
    lines = out.splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if ln.startswith("#@need service:postgres"))
    body = lines[start:]
    assert any("(no command" in ln for ln in body)
    for ln in body:
        if ln.strip():
            assert ln.startswith("#"), f"non-comment line in #@need stub: {ln!r}"

    # exhaustively prove EVERY #@need stub (config + service) is comment-only:
    # anchor at the first #@need line — from there to EOF there must be no
    # executable line (every non-blank line starts with '#').
    first_need = next(i for i, ln in enumerate(lines) if ln.startswith("#@need "))
    for ln in lines[first_need:]:
        if ln.strip():
            assert ln.startswith("#"), f"non-comment line in #@need region: {ln!r}"


def test_manual_block_renders_and_suppresses_its_need():
    g = DepGraph(nodes=(
        _need("service:postgres", NodeType.SERVICE, "postgres", Layer.SERVICES,
              check_command="pg_isready -q"),
    ))
    blk = Block(
        block_id="svc:postgres-init", wave="services",
        commands=("pg_ctl init && pg_ctl start",),
        target_node_ids=("service:postgres",),
        check_commands=("pg_isready -q",),
        evidence_refs=("ev:readme:db",),
    )
    out = render_build_script(g, manual_blocks=(blk,))
    # the LLM block is rendered with provenance
    assert ("#@block svc:postgres-init  source=llm-patch  "
            "targets=service:postgres") in out
    assert "pg_ctl init && pg_ctl start" in out
    # the covered node is NOT also a #@need
    assert "#@need service:postgres" not in out


def test_uncovered_need_still_stubbed_with_block_present():
    g = DepGraph(nodes=(
        _need("config:DATABASE_URL", NodeType.CONFIG, "DATABASE_URL", Layer.CONFIG),
    ))
    blk = Block(block_id="svc:x", wave="services", commands=("true",),
                target_node_ids=("service:other",))
    out = render_build_script(g, manual_blocks=(blk,))
    assert "#@need config:DATABASE_URL" in out


def test_block_appears_in_its_wave_section_after_pip():
    g = DepGraph(nodes=(
        _pkg("pkg:psycopg2", "psycopg2", "2.9.9"),
        _need("service:postgres", NodeType.SERVICE, "postgres", Layer.SERVICES),
    ))
    blk = Block(block_id="svc:pg-init", wave="services",
                commands=("pg_ctl start",), target_node_ids=("service:postgres",))
    out = render_build_script(g, manual_blocks=(blk,))
    assert out.index("psycopg2==2.9.9") < out.index("pg_ctl start")


def test_block_with_empty_targets_renders_and_covers_nothing():
    g = DepGraph(nodes=(
        _need("config:DATABASE_URL", NodeType.CONFIG, "DATABASE_URL", Layer.CONFIG),
    ))
    blk = Block(block_id="meta:setup", wave="config", commands=("echo setup",),
                target_node_ids=())
    out = render_build_script(g, manual_blocks=(blk,))
    assert "#@block meta:setup" in out
    assert "echo setup" in out
    assert "#@need config:DATABASE_URL" in out          # empty targets -> no coverage


def test_block_with_unknown_wave_raises():
    import pytest
    from python_deps.depgraph.block import Block
    blk = Block(block_id="blk:x", wave="post-install", commands=("echo hi",),
                target_node_ids=(), provider_ids=(), check_commands=(), evidence_refs=())
    with pytest.raises(ValueError, match="illegal waves"):
        render_build_script(DepGraph(), (blk,))


def test_manifest_counts_hash_and_meta():
    g = DepGraph(nodes=(
        _apt("syslib:libpq-dev", "libpq-dev", "apt:libpq-dev"),
        _pkg("pkg:psycopg2", "psycopg2", "2.9.9",
             resolved_python="3.11", resolved_platform="linux/amd64",
             exclude_newer="2026-06-01"),
        _need("service:postgres", NodeType.SERVICE, "postgres", Layer.SERVICES),
    ))
    out = render_build_script(g)
    preamble = out[:out.index("set -Eeuo pipefail")]
    assert "#   nodes: 2 reciped (1 system, 1 pip) + 1 needs (1 service)" in preamble
    assert "#   graph-hash: sha256:" in preamble
    # meta fields live in the comment header, before the set line (not in body)
    for needle in ("python: 3.11", "platform: linux/amd64", "exclude-newer: 2026-06-01"):
        assert any(needle in ln and ln.startswith("#")
                   for ln in preamble.splitlines()), needle


def test_determinism_with_mixed_tier_insertion_order():
    nodes = (
        _apt("syslib:libpq-dev", "libpq-dev", "apt:libpq-dev"),
        _pkg("pkg:psycopg2", "psycopg2", "2.9.9"),
        _apt("tool:gcc", "gcc", "apt:gcc", type_=NodeType.TOOL, layer=Layer.TOOLCHAIN),
    )
    g1 = DepGraph(nodes=nodes)
    g2 = DepGraph(nodes=tuple(reversed(nodes)))
    assert render_build_script(g1) == render_build_script(g2)   # insertion-order invariant
    assert render_build_script(g1) == render_build_script(g1)   # pure: same in, same out


def test_closure_meta_is_insertion_order_invariant():
    a = _pkg("pkg:aaa", "aaa", "1.0", resolved_python="3.10")
    b = _pkg("pkg:zzz", "zzz", "2.0", resolved_python="3.11")
    g1 = DepGraph(nodes=(a, b))
    g2 = DepGraph(nodes=(b, a))
    assert render_build_script(g1) == render_build_script(g2)
    # lowest-id package wins deterministically -> python 3.10
    assert "python: 3.10" in render_build_script(g1)


def _rich_graph():
    g = DepGraph(nodes=(
        _apt("syslib:libpq-dev", "libpq-dev", "apt:libpq-dev"),
        _apt("tool:gcc", "gcc", "apt:gcc", type_=NodeType.TOOL, layer=Layer.TOOLCHAIN,
             evidence="ev:build:psycopg2"),
        _pkg("pkg:typing-extensions", "typing-extensions", "4.11.0",
             evidence="ev:resolver"),
        _pkg("pkg:psycopg2", "psycopg2", "2.9.9", build_from_source=True,
             evidence="ev:import:psycopg2"),
        _need("service:postgres", NodeType.SERVICE, "postgres", Layer.SERVICES,
              check_command="pg_isready -q", evidence="ev:readme:db"),
        _need("config:DATABASE_URL", NodeType.CONFIG, "DATABASE_URL", Layer.CONFIG,
              evidence="ev:settings:DATABASE_URL"),
    ))
    for src, dst in (("pkg:psycopg2", "syslib:libpq-dev"),
                     ("pkg:psycopg2", "tool:gcc")):
        g = g.with_edge(Edge(src=src, dst=dst, relation=EdgeType.REQUIRES))
    return g


def test_every_reciped_node_installed_exactly_once():
    g = _rich_graph()
    out = render_build_script(g)
    for n in g.nodes:
        if not _is_reciped(n):
            continue
        if _apt_name(n) is not None:
            cmd = f"apt-get install -y --no-install-recommends {_apt_name(n)}"
        else:
            cmd = (f"python3 -m pip install --break-system-packages --no-deps "
                   f"{_pip_spec(n)}")
        assert out.count(cmd) == 1, f"{n.id}: expected 1 install line, got {out.count(cmd)}"


def test_build_from_source_and_toolchain_flags_in_annotations():
    out = render_build_script(_rich_graph())
    psycopg2_line = next(ln for ln in out.splitlines()
                         if ln.startswith("#@node pkg:psycopg2"))
    assert "build-from-source" in psycopg2_line
    gcc_line = next(ln for ln in out.splitlines()
                    if ln.startswith("#@node tool:gcc"))
    assert "toolchain" in gcc_line


def test_requires_edge_orders_lines():
    out = render_build_script(_rich_graph())
    assert out.index("libpq-dev\n") < out.index("psycopg2==2.9.9")
    assert out.index("gcc\n") < out.index("psycopg2==2.9.9")


def test_install_target_parity_with_compile_replay_blocks():
    g = _rich_graph()
    replay_targets = {nid for b in compile_replay_blocks(g) for nid in b.target_node_ids}
    out = render_build_script(g)
    # every replay target appears as a #@node annotation in the artifact
    for nid in replay_targets:
        assert f"#@node {nid}" in out
    # and the artifact introduces no extra #@node beyond the reciped set
    node_ids = {ln.split()[1] for ln in out.splitlines() if ln.startswith("#@node ")}
    assert node_ids == replay_targets


def test_golden_snapshot_byte_for_byte():
    out = render_build_script(_rich_graph())
    # mask the opaque digest (its value is covered by the determinism test)
    normalized = re.sub(r"sha256:[0-9a-f]{12}", "sha256:<HASH>", out)
    expected = (
        "#!/usr/bin/env bash\n"
        "#\n"
        "# setup.sh — COMPILED from the certified dependency graph. DO NOT EDIT.\n"
        "# Edit the graph and re-render; this file is an artifact, not a source.\n"
        "#\n"
        "#   nodes: 4 reciped (1 system, 1 toolchain, 2 pip) + 2 needs (1 service, 1 config)\n"
        "#   graph-hash: sha256:<HASH>\n"
        "#\n"
        "set -Eeuo pipefail\n"
        "\n"
        "# Normalize `python` -> python3 so bare-`python` checks (pip show / pytest) resolve.\n"
        'command -v python >/dev/null 2>&1 || ln -sf "$(command -v python3)" /usr/local/bin/python\n'
        "\n"
        "# Ensure the pytest test-runner (testability-gate precondition; not a graph node).\n"
        'python3 -c "import pytest" >/dev/null 2>&1 || python3 -m pip install --break-system-packages pytest\n'
        "\n"
        "# ==================== SYSTEM ====================\n"
        "export DEBIAN_FRONTEND=noninteractive\n"
        "apt-get update\n"
        "#@node syslib:libpq-dev  provider=apt:libpq-dev  requires=-  unblocks=pkg:psycopg2\n"
        "apt-get install -y --no-install-recommends libpq-dev\n"
        "\n"
        "# ==================== TOOLCHAIN ====================\n"
        "#@node tool:gcc  provider=apt:gcc  requires=-  unblocks=pkg:psycopg2  toolchain  evidence=ev:build:psycopg2\n"
        "apt-get install -y --no-install-recommends gcc\n"
        "\n"
        "# ==================== PIP ====================\n"
        "#@node pkg:psycopg2  version=2.9.9  requires=syslib:libpq-dev,tool:gcc  build-from-source  evidence=ev:import:psycopg2\n"
        "python3 -m pip install --break-system-packages --no-deps psycopg2==2.9.9\n"
        "#@node pkg:typing-extensions  version=4.11.0  requires=-  evidence=ev:resolver\n"
        "python3 -m pip install --break-system-packages --no-deps typing-extensions==4.11.0\n"
        "\n"
        "# ==================== CONFIG ====================\n"
        "#\n"
        "#@need config:DATABASE_URL  state=missing\n"
        "#@evidence ev:settings:DATABASE_URL\n"
        "#     (no command — propose a governed block to satisfy this)\n"
        "\n"
        "# ==================== SERVICES ====================\n"
        "#\n"
        "#@need service:postgres  state=missing\n"
        "#@check pg_isready -q\n"
        "#@evidence ev:readme:db\n"
        "#     (no command — propose a governed block to satisfy this)\n"
    )
    assert normalized == expected


def test_python_shim_baked_after_pipefail():
    out = render_build_script(_rich_graph())
    shim = 'command -v python >/dev/null 2>&1 || ln -sf "$(command -v python3)" /usr/local/bin/python'
    assert out.count(shim) == 1
    assert out.index("set -Eeuo pipefail") < out.index(shim)
    # shim precedes the first section header (runs before any node install/check)
    assert out.index(shim) < out.index("# ====================")


def test_python_shim_present_for_empty_graph():
    for g in (None, DepGraph()):
        out = render_build_script(g)
        assert 'ln -sf "$(command -v python3)" /usr/local/bin/python' in out


def test_runtime_and_interpreter_precede_pip():
    # base python (RUNTIME) + interpreter floor must be laid down BEFORE pip installs
    assert _LAYER_ORDER.index(Layer.RUNTIME) < _LAYER_ORDER.index(Layer.PIP)
    assert _LAYER_ORDER.index(Layer.INTERPRETER) < _LAYER_ORDER.index(Layer.PIP)


def test_config_precedes_tests():
    # env/config tier must be set up before the test tier runs
    assert _LAYER_ORDER.index(Layer.CONFIG) < _LAYER_ORDER.index(Layer.TESTS)


def test_build_script_order_agrees_with_certify_on_shared_tiers():
    from python_deps.depgraph.certify import EXECUTION_LAYER_ORDER  # created in Step 3

    positions = [_LAYER_ORDER.index(L) for L in EXECUTION_LAYER_ORDER]
    assert positions == sorted(positions), (
        "build_script section order must not contradict certify execution order")
