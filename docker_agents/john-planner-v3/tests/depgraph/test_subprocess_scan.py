"""TDD for subprocess_scan: discover the external CLI tools a repo shells out to
(finding C — ldd/apt-on-build never sees a program the code only subprocess-es).
"""
from python_deps.depgraph.ids import TEST_NODE_ID, project_id, tool_id
from python_deps.depgraph.schema import (
    DepGraph, DiscoveredBy, EdgeType, Layer, Node, NodeType, State,
)
from python_deps.depgraph.subprocess_scan import (
    add_subprocess_tool_nodes,
    scan_subprocess_tools,
)


def _write(tmp_path, rel, body):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


# --------------------------------------------------------------------------- #
# scan_subprocess_tools — pure AST discovery                                   #
# --------------------------------------------------------------------------- #

def test_detects_list_argv_subprocess_calls(tmp_path):
    _write(tmp_path, "app.py", (
        "import subprocess\n"
        "subprocess.run(['adb', 'devices'])\n"
        "subprocess.Popen(['git', 'clone', url])\n"
        "subprocess.check_output(['java', '-version'])\n"
    ))
    found = scan_subprocess_tools(str(tmp_path))
    assert set(found) == {"adb", "git", "java"}
    assert "app.py:2" in found["adb"]  # evidence carries file:line


def test_detects_os_system_and_shutil_which_and_shell_string(tmp_path):
    _write(tmp_path, "pkg/run.py", (
        "import os, shutil, subprocess\n"
        "os.system('sqlite3 db.sqlite < schema.sql')\n"
        "shutil.which('ffmpeg')\n"
        "subprocess.run('git status', shell=True)\n"
    ))
    assert set(scan_subprocess_tools(str(tmp_path))) == {"sqlite3", "ffmpeg", "git"}


def test_strips_absolute_path_to_basename(tmp_path):
    _write(tmp_path, "a.py", "import subprocess\nsubprocess.run(['/usr/bin/adb', 'shell'])\n")
    assert set(scan_subprocess_tools(str(tmp_path))) == {"adb"}


def test_ignores_unknown_tools_builtins_and_local_scripts(tmp_path):
    # none of these are on the curated allowlist -> must NOT be flagged.
    _write(tmp_path, "a.py", (
        "import os, subprocess\n"
        "subprocess.run(['mycli', 'go'])\n"        # unknown external tool
        "subprocess.run(['cd', '/tmp'])\n"          # shell builtin
        "subprocess.run(['./scripts/build.sh'])\n"  # project-local script
        "os.system('echo hi')\n"                    # coreutil
        "subprocess.run([sys.executable, '-m', 'pip'])\n"  # non-literal argv0
        "subprocess.run(cmd)\n"                     # variable command
    ))
    assert scan_subprocess_tools(str(tmp_path)) == {}


def test_non_utf8_file_does_not_crash_scan(tmp_path):
    # A latin-1 byte (0xe9) is invalid UTF-8; reading it raises UnicodeDecodeError
    # (a ValueError, NOT an OSError). The scan must skip the file, not crash the
    # whole graph build — a cosmetic encoding issue in ONE unrelated file must not
    # discard the entire dependency graph.
    (tmp_path / "bad.py").write_bytes(b"# comment with a latin-1 byte: \xe9\n")
    _write(tmp_path, "good.py", "import subprocess\nsubprocess.run(['git', 'x'])\n")
    found = scan_subprocess_tools(str(tmp_path))
    assert found == {"git": found.get("git", "")}  # good.py still scanned
    assert "git" in found


def test_tables_are_disjoint():
    # id-collision guard: CLI_TOOL_TO_APT (subprocess tools, id=tool:<name>) and
    # the resolver's build-tool binaries (os_resolver.PROVIDER_TABLE, id via
    # tool:<apt>) MUST stay disjoint so a future edit can't silently mint two
    # nodes for one apt package. (The old build-tool half was retired into
    # PROVIDER_TABLE; this now guards CLI tools vs the single apt authority.)
    from python_deps.depgraph.os_resolver import PROVIDER_TABLE
    from python_deps.depgraph.tables import CLI_TOOL_TO_APT
    provider_binaries = {name for (kind, name) in PROVIDER_TABLE if kind == "binary"}
    assert not (set(CLI_TOOL_TO_APT) & provider_binaries)


def test_ignores_calls_in_excluded_dirs(tmp_path):
    _write(tmp_path, "examples/demo.py", "import subprocess\nsubprocess.run(['git', 'x'])\n")
    _write(tmp_path, "docs/gen.py", "import os\nos.system('adb devices')\n")
    assert scan_subprocess_tools(str(tmp_path)) == {}


def test_non_subprocess_call_with_list_literal_is_ignored(tmp_path):
    # a data literal that merely happens to contain a tool name is not a call.
    _write(tmp_path, "a.py", "COMMANDS = ['git', 'adb']\nprint(['sqlite3'])\n")
    assert scan_subprocess_tools(str(tmp_path)) == {}


# --------------------------------------------------------------------------- #
# add_subprocess_tool_nodes — graph wiring                                     #
# --------------------------------------------------------------------------- #

def _project_graph():
    proj = Node(id=project_id("mvt"), type=NodeType.PROJECT, name="mvt",
                layer=Layer.PIP, discovered_by=DiscoveredBy.STATIC_SCAN,
                state=State.UNKNOWN, data={"installable": True})
    return DepGraph(nodes=(proj,))


def test_wires_tool_nodes_with_apt_fix_and_check_and_project_edge(tmp_path):
    _write(tmp_path, "mvt/adb.py", "import subprocess\nsubprocess.run(['adb', 'devices'])\n")
    out = add_subprocess_tool_nodes(_project_graph(), str(tmp_path))
    node = out.get(tool_id("adb"))
    assert node is not None
    assert node.type is NodeType.TOOL
    assert node.layer is Layer.TOOLCHAIN
    assert node.chosen_fix == "apt:adb"
    assert node.check_command == "command -v adb"
    assert node.discovered_by is DiscoveredBy.STATIC_SCAN
    # requires edge from the Project anchor (the repo's code invokes it)
    assert any(e.src == project_id("mvt") and e.dst == tool_id("adb")
               and e.relation is EdgeType.REQUIRES for e in out.edges)


def test_java_maps_to_default_jre(tmp_path):
    _write(tmp_path, "m/x.py", "import subprocess\nsubprocess.check_call(['java', '-jar', p])\n")
    out = add_subprocess_tool_nodes(_project_graph(), str(tmp_path))
    assert out.get(tool_id("java")).chosen_fix == "apt:default-jre-headless"


def test_anchor_falls_back_to_test_node_without_project(tmp_path):
    _write(tmp_path, "x.py", "import subprocess\nsubprocess.run(['git', 'log'])\n")
    test = Node(id=TEST_NODE_ID, type=NodeType.TEST, name="repo_tests_pass",
                layer=Layer.TESTS, discovered_by=DiscoveredBy.GOAL)
    out = add_subprocess_tool_nodes(DepGraph(nodes=(test,)), str(tmp_path))
    assert any(e.src == TEST_NODE_ID and e.dst == tool_id("git") for e in out.edges)


def test_noop_when_no_tools_found(tmp_path):
    _write(tmp_path, "x.py", "print('hello')\n")
    g = _project_graph()
    out = add_subprocess_tool_nodes(g, str(tmp_path))
    assert out is g  # unchanged identity — pure no-op


def test_unknown_tool_never_becomes_a_node(tmp_path):
    _write(tmp_path, "x.py", "import subprocess\nsubprocess.run(['mycli', 'go'])\n")
    out = add_subprocess_tool_nodes(_project_graph(), str(tmp_path))
    assert out.get(tool_id("mycli")) is None
    assert all(n.type is not NodeType.TOOL for n in out.nodes)
