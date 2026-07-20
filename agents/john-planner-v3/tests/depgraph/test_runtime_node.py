from conftest import FakeExecutor
from python_deps.depgraph.build import build_dep_graph
from python_deps.depgraph.executor import CommandResult
from python_deps.depgraph.ids import runtime_id
from python_deps.depgraph.schema import NodeType, Layer


def test_runtime_id_is_stable():
    assert runtime_id("3.10") == "runtime:python-3.10"


def test_build_adds_a_runtime_node(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nrequires-python = ">=3.10"\n')
    ex = FakeExecutor(default=CommandResult(command="", returncode=0, stdout="", stderr=""))
    g = build_dep_graph(str(tmp_path), ex, host_executor=ex, target_python="3.10")
    rt = [n for n in g.nodes if n.type is NodeType.RUNTIME]
    assert len(rt) == 1
    assert rt[0].id == "runtime:python-3.10"
    assert rt[0].layer is Layer.RUNTIME
    assert rt[0].version == "3.10"
    assert "sys.version_info[:2]==(3,10)" in rt[0].check_command.replace(" ", "")
