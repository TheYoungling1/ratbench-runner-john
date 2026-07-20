# tests/depgraph/test_script_render.py
from python_deps.depgraph.block import Block
from python_deps.depgraph.script import render_setup_sh, parse_setup_sh

_B = (
    Block(block_id="system.libpq", wave="system",
          commands=("apt-get install -y --no-install-recommends libpq-dev",),
          target_node_ids=("syslib:libpq",), provider_ids=("apt:libpq-dev",),
          check_commands=("ldconfig -p | grep -q libpq",)),
    Block(block_id="python.psycopg2", wave="python",
          commands=("python3 -m pip install --break-system-packages psycopg2==2.9.9",),
          target_node_ids=("pkg:psycopg2",),
          check_commands=("python -m pip show psycopg2",)),
)


def test_headers_and_strict_mode():
    out = render_setup_sh(_B)
    assert out.splitlines()[0].startswith("#!")          # shebang
    assert "set -Eeuo pipefail" in out
    assert out.count("#@action") == 2
    assert "#@targets syslib:libpq" in out
    assert "#@provides apt:libpq-dev" in out
    assert "#@check ldconfig -p | grep -q libpq" in out


def test_render_parse_roundtrip():
    assert parse_setup_sh(render_setup_sh(_B)) == _B
