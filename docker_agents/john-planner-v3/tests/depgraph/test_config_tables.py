from python_deps.depgraph.config_tables import config_obligations_for_package


def test_django_induces_settings_module():
    obligations = config_obligations_for_package("django")
    assert ("DJANGO_SETTINGS_MODULE", None) in obligations


def test_lookup_is_name_normalized():
    assert config_obligations_for_package("Django") == config_obligations_for_package("django")


def test_unknown_package_returns_empty_list():
    assert config_obligations_for_package("requests") == []


def test_returns_fresh_list():
    a = config_obligations_for_package("django")
    a.append(("X", None))
    assert config_obligations_for_package("django") != a  # caller mutation isolated
