# tests/depgraph/test_action_class.py
from python_deps.depgraph.action_class import matches_action_class, ACTION_CLASSES


def test_apt_class():
    assert matches_action_class("apt", "apt-get install -y --no-install-recommends libpq-dev")
    assert matches_action_class("apt", "apt-get update && apt-get install -y libpq-dev")
    # §14 "wrong action class" case: kind=apt but command is not apt-get install
    assert not matches_action_class("apt", "pip install psycopg2")


def test_pip_class():
    assert matches_action_class("pip", "pip install psycopg2==2.9.9")
    assert matches_action_class("pip", "python3 -m pip install --break-system-packages psycopg2")
    assert not matches_action_class("pip", "apt-get install python3-psycopg2")


def test_npm_class():
    assert matches_action_class("npm", "npm install")
    assert matches_action_class("npm", "npm ci")
    assert not matches_action_class("npm", "yarn add foo")


def test_shell_is_explicit_escape_hatch():
    assert matches_action_class("shell", "make && make install")
    assert "shell" in ACTION_CLASSES


def test_unknown_kind_rejected_and_empty_rejected():
    assert not matches_action_class("brew", "brew install foo")
    assert not matches_action_class("shell", "")
