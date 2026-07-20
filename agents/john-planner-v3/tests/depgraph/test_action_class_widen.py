from python_deps.depgraph.action_class import matches_action_class

def test_pip3_and_apt_frontend():
    assert matches_action_class("pip", "pip3 install lxml") is True
    assert matches_action_class("apt", "apt install -y libpq-dev") is True

def test_existing_still_match():
    assert matches_action_class("apt", "apt-get install -y libpq-dev") is True
    assert matches_action_class("pip", "python3 -m pip install lxml") is True
    assert matches_action_class("npm", "npm ci") is True
