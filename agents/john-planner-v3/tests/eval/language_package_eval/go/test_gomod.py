from __future__ import annotations

from src.eval.language_package_eval.go.gomod import (
    Exclude,
    Replace,
    Require,
    parse_go_mod,
)

GOMOD_BLOCK = """\
module github.com/acme/app

go 1.21

toolchain go1.21.4

require (
    github.com/spf13/cobra v1.8.0
    github.com/inconshreveable/mousetrap v1.1.0 // indirect
)

require github.com/spf13/pflag v1.0.5

replace github.com/old/mod => github.com/new/mod v1.5.0
replace github.com/local/mod => ../local

exclude github.com/bad/mod v1.1.0
"""


def test_parse_go_mod_full(tmp_path):
    p = tmp_path / "go.mod"
    p.write_text(GOMOD_BLOCK)
    gm = parse_go_mod(p)

    assert gm.module_path == "github.com/acme/app"
    assert gm.go_version == "1.21"
    assert gm.toolchain == "go1.21.4"
    assert Require("github.com/spf13/cobra", "v1.8.0", False) in gm.requires
    assert (
        Require("github.com/inconshreveable/mousetrap", "v1.1.0", True) in gm.requires
    )
    assert Require("github.com/spf13/pflag", "v1.0.5", False) in gm.requires
    assert (
        Replace("github.com/old/mod", None, "github.com/new/mod", "v1.5.0")
        in gm.replaces
    )
    assert Replace("github.com/local/mod", None, "../local", None) in gm.replaces
    assert Exclude("github.com/bad/mod", "v1.1.0") in gm.excludes


def test_parse_go_mod_no_go_directive(tmp_path):
    (tmp_path / "go.mod").write_text("module github.com/x/y\n")
    gm = parse_go_mod(tmp_path / "go.mod")
    assert gm.go_version == ""
    assert gm.requires == ()


from src.eval.language_package_eval.go.gomod import (  # noqa: E402
    parse_go_sum,
    parse_go_work,
    parse_vendor_modules_txt,
)

MODULES_TXT = """\
# github.com/spf13/cobra v1.8.0
## explicit; go 1.15
github.com/spf13/cobra
# github.com/spf13/pflag v1.0.5
## explicit; go 1.12
github.com/spf13/pflag/...
"""


def test_parse_vendor_modules_txt(tmp_path):
    p = tmp_path / "modules.txt"
    p.write_text(MODULES_TXT)
    assert parse_vendor_modules_txt(p) == {
        "github.com/spf13/cobra": "v1.8.0",
        "github.com/spf13/pflag": "v1.0.5",
    }


def test_parse_go_sum_strips_gomod_suffix(tmp_path):
    p = tmp_path / "go.sum"
    p.write_text(
        "github.com/spf13/cobra v1.8.0 h1:aaa=\n"
        "github.com/spf13/cobra v1.8.0/go.mod h1:bbb=\n"
    )
    assert parse_go_sum(p) == frozenset({("github.com/spf13/cobra", "v1.8.0")})


def test_parse_go_work_block_and_single(tmp_path):
    (tmp_path / "go.work").write_text(
        "go 1.21\n\nuse (\n    ./a\n    ./b\n)\n\nuse ./c\n"
    )
    assert parse_go_work(tmp_path / "go.work") == ("./a", "./b", "./c")


def test_parse_go_work_missing_returns_empty(tmp_path):
    assert parse_go_work(tmp_path / "go.work") == ()


from src.eval.language_package_eval.go.gomod import (
    module_closure,
)  # noqa: E402


def _repo(tmp_path, gomod_text, name="repo"):
    d = tmp_path / name
    d.mkdir()
    (d / "go.mod").write_text(gomod_text)
    return d


def test_closure_pruned_excludes_main_and_counts(tmp_path):
    d = _repo(
        tmp_path,
        "module github.com/acme/app\n\ngo 1.21\n\n"
        "require (\n    github.com/spf13/cobra v1.8.0\n"
        "    github.com/x/y v0.1.0 // indirect\n)\n",
    )
    c = module_closure(d)
    assert c.source == "gomod-pruned"
    assert c.packages == {
        "github.com/spf13/cobra": "v1.8.0",
        "github.com/x/y": "v0.1.0",
    }
    assert c.direct == 1 and c.indirect == 1
    assert c.resolve_required is False


def test_closure_pre_1_17_is_resolve_required(tmp_path):
    d = _repo(
        tmp_path,
        "module github.com/acme/old\n\ngo 1.16\n\n"
        "require github.com/spf13/cobra v1.8.0\n",
    )
    c = module_closure(d)
    assert c.source == "resolve-required"
    assert c.packages == {}
    assert c.resolve_required is True


def test_closure_registry_replace_rewrites_version(tmp_path):
    d = _repo(
        tmp_path,
        "module m\n\ngo 1.21\n\nrequire github.com/x/y v1.0.0\n"
        "replace github.com/x/y => github.com/fork/y v1.2.0\n",
    )
    c = module_closure(d)
    assert c.packages == {"github.com/x/y": "v1.2.0"}
    assert c.replace_local == ()


def test_closure_registry_replace_respects_old_version(tmp_path):
    # `replace X vOld => Y vN` applies ONLY when the selected version == vOld.
    # Here selected is v1.0.0 but the replace names v0.9.0 -> it must be a no-op.
    d = _repo(
        tmp_path,
        "module m\n\ngo 1.21\n\nrequire github.com/x/y v1.0.0\n"
        "replace github.com/x/y v0.9.0 => github.com/fork/y v1.2.0\n",
    )
    c = module_closure(d)
    assert c.packages == {"github.com/x/y": "v1.0.0"}  # unchanged


def test_closure_local_replace_dropped_and_recorded(tmp_path):
    d = _repo(
        tmp_path,
        "module m\n\ngo 1.21\n\nrequire github.com/x/y v1.0.0\n"
        "replace github.com/x/y => ../local\n",
    )
    c = module_closure(d)
    assert c.packages == {}
    assert c.replace_local == ("github.com/x/y",)


def test_closure_exclude_matching_version_taints(tmp_path):
    # `exclude` FORBIDS a version; MVS re-selects. We cannot compute the next
    # version offline, so an exclude of the selected version taints to
    # resolve-required (spec §3.1) — it does NOT silently drop the module.
    d = _repo(
        tmp_path,
        "module m\n\ngo 1.21\n\nrequire github.com/x/y v1.0.0\n"
        "exclude github.com/x/y v1.0.0\n",
    )
    c = module_closure(d)
    assert c.source == "resolve-required"
    assert c.resolve_required is True


def test_closure_exclude_nonmatching_version_is_noop(tmp_path):
    # excluding a version other than the selected one changes nothing.
    d = _repo(
        tmp_path,
        "module m\n\ngo 1.21\n\nrequire github.com/x/y v1.0.0\n"
        "exclude github.com/x/y v0.9.0\n",
    )
    c = module_closure(d)
    assert c.packages == {"github.com/x/y": "v1.0.0"}
    assert c.resolve_required is False


def test_closure_replace_cannot_mask_excluded_selected_version(tmp_path):
    # exclude affects SELECTION (pre-replace); an unconditional replace must NOT
    # hide that the selected v1.0.0 is excluded -> taint to resolve-required.
    d = _repo(
        tmp_path,
        "module m\n\ngo 1.21\n\nrequire github.com/x/y v1.0.0\n"
        "replace github.com/x/y => github.com/fork/y v2.0.0\n"
        "exclude github.com/x/y v1.0.0\n",
    )
    c = module_closure(d)
    assert c.source == "resolve-required"
    assert c.resolve_required is True


def test_closure_vendor_wins_over_gomod(tmp_path):
    d = _repo(tmp_path, "module m\n\ngo 1.13\n\nrequire github.com/x/y v1.0.0\n")
    (d / "vendor").mkdir()
    (d / "vendor" / "modules.txt").write_text("# github.com/x/y v1.0.0\n## explicit\n")
    c = module_closure(d)
    assert c.source == "vendor"
    assert c.packages == {"github.com/x/y": "v1.0.0"}


def test_closure_workspace_merges_members(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "go.work").write_text("go 1.21\n\nuse (\n    ./a\n    ./b\n)\n")
    a = ws / "a"
    a.mkdir()
    (a / "go.mod").write_text(
        "module example.com/a\n\ngo 1.21\n\nrequire github.com/x/y v1.0.0\n"
    )
    b = ws / "b"
    b.mkdir()
    (b / "go.mod").write_text(
        "module example.com/b\n\ngo 1.21\n\nrequire github.com/z/w v2.0.0\n"
    )
    c = module_closure(ws)
    assert c.source == "workspace"
    assert c.packages == {"github.com/x/y": "v1.0.0", "github.com/z/w": "v2.0.0"}


def test_closure_workspace_max_version_wins(tmp_path):
    # Two members require the SAME module at different versions. Go runs one
    # global MVS across the workspace -> the MAX version wins, NOT last-write.
    ws = tmp_path / "wsmax"
    ws.mkdir()
    (ws / "go.work").write_text("go 1.21\n\nuse (\n    ./a\n    ./b\n)\n")
    a = ws / "a"
    a.mkdir()
    (a / "go.mod").write_text(
        "module example.com/a\n\ngo 1.21\n\nrequire github.com/x/y v1.2.0\n"
    )
    b = ws / "b"
    b.mkdir()
    (b / "go.mod").write_text(
        "module example.com/b\n\ngo 1.21\n\nrequire github.com/x/y v1.10.0\n"
    )
    c = module_closure(ws)
    assert c.packages == {"github.com/x/y": "v1.10.0"}  # 1.10 > 1.2, not string-last


def test_closure_workspace_missing_member_taints(tmp_path):
    ws = tmp_path / "ws2"
    ws.mkdir()
    (ws / "go.work").write_text("go 1.21\n\nuse ./missing\n")
    c = module_closure(ws)
    assert c.source == "resolve-required"
    assert c.resolve_required is True


def test_closure_workspace_level_replace_taints(tmp_path):
    # go.work `replace` (and go.work.sum) are not modeled in slice 1 -> taint.
    ws = tmp_path / "ws3"
    ws.mkdir()
    (ws / "go.work").write_text(
        "go 1.21\n\nuse ./a\n\nreplace github.com/x/y => ../fork\n"
    )
    a = ws / "a"
    a.mkdir()
    (a / "go.mod").write_text(
        "module example.com/a\n\ngo 1.21\n\nrequire github.com/x/y v1.0.0\n"
    )
    c = module_closure(ws)
    assert c.source == "resolve-required"
    assert c.resolve_required is True


def test_closure_workspace_use_dot_does_not_recurse(tmp_path):
    # `use .` -> the workspace member IS the root (which has go.work). Must resolve
    # via the go.mod ladder, not re-enter the workspace branch and RecursionError.
    ws = tmp_path / "wsdot"
    ws.mkdir()
    (ws / "go.work").write_text("go 1.21\n\nuse .\n")
    (ws / "go.mod").write_text(
        "module example.com/root\n\ngo 1.21\n\nrequire github.com/x/y v1.0.0\n"
    )
    c = module_closure(ws)
    assert c.source == "workspace"
    assert c.packages == {"github.com/x/y": "v1.0.0"}


from src.eval.language_package_eval.go.run_ours_go import ours_for_repo  # noqa: E402


def test_ours_for_repo_shape(tmp_path):
    d = _repo(
        tmp_path,
        "module github.com/acme/app\n\ngo 1.21\n\n"
        "require github.com/spf13/cobra v1.8.0\n",
    )
    rec = ours_for_repo(d)
    assert rec["packages"] == {"github.com/spf13/cobra": "v1.8.0"}
    assert rec["package_count"] == 1
    assert rec["closure_source"] == "gomod-pruned"
    assert rec["project"] == "github.com/acme/app"
    assert rec["resolve_required"] is False
    assert rec["target"] == {"goos": "linux", "goarch": "amd64"}
