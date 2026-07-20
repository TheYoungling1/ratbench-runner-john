# tests/test_world_model_system_field.py
from src.envstate.world_model import initial_map, merge_map, map_to_dict, map_from_dict, Fact


def _base():
    return initial_map(base_image="python:3.12", workdir="/app", language="python",
                       build_system="pip", repo_layout=())


def test_system_installed_defaults_empty():
    assert _base().system_installed == ()


def test_merge_replaces_and_defensive_copies_tuple():
    m = merge_map(_base(), system_installed=(Fact("libpq-dev", "dpkg"), Fact("pg_config", "tool")))
    assert {f.name for f in m.system_installed} == {"libpq-dev", "pg_config"}
    assert _base().system_installed == ()  # original frozen/unchanged


def test_system_installed_round_trips():
    m = merge_map(_base(), system_installed=(Fact("gcc", "tool"),))
    assert map_from_dict(map_to_dict(m)).system_installed == (Fact("gcc", "tool"),)
