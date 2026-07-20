import python_deps.depgraph.build_script as bs
from python_deps.depgraph.build_script import render_build_script
from python_deps.depgraph.populate import populate_setup_commands
from python_deps.depgraph.schema import (
    DepGraph, DiscoveredBy, Layer, Node, NodeType, State,
)


def _pkg(setup_commands=()):
    return Node(id="pkg:requests", type=NodeType.PACKAGE, name="requests",
                layer=Layer.PIP, discovered_by=DiscoveredBy.RESOLVER,
                version="2.0", state=State.MISSING, chosen_fix="pip:requests",
                setup_commands=setup_commands)


def test_install_command_is_deleted():
    # The single-producer rule: build_script no longer derives commands.
    assert not hasattr(bs, "_install_command")


def test_renderer_emits_setup_commands_verbatim():
    # A node whose setup_commands differ from any derivation proves the renderer
    # reads the field. (populate IS called inside render, but its idempotency
    # guard skips this node since setup_commands is already set.)
    g = DepGraph(nodes=(_pkg(setup_commands=("echo CUSTOM_INSTALL",)),))
    script = render_build_script(g)
    assert "echo CUSTOM_INSTALL" in script
    # the custom command is used verbatim, not replaced by a derived pip install
    # for this node; ignore the global pytest-runner precondition line (a gate
    # precondition baked into every setup.sh, not a node install).
    assert "pip install" not in script.replace(
        "python3 -m pip install --break-system-packages pytest", "")


def test_render_auto_populates_reciped_nodes():
    # No setup_commands on input -> render populates internally -> pinned pip line.
    g = DepGraph(nodes=(_pkg(),))
    script = render_build_script(g)
    assert "python3 -m pip install --break-system-packages --no-deps requests==2.0" in script


def test_render_is_byte_identical_to_explicit_populate():
    syslib = Node(id="syslib:libpq", type=NodeType.SYSTEM_LIB, name="libpq",
                  layer=Layer.SYSTEM, discovered_by=DiscoveredBy.RESOLVER,
                  state=State.MISSING, chosen_fix="apt:libpq-dev")
    g = DepGraph(nodes=(_pkg(), syslib))
    assert render_build_script(g) == render_build_script(populate_setup_commands(g))
