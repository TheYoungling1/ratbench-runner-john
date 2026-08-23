# tests/test_packaging_importable.py
def test_manifest_deps_importable():
    import tomllib            # stdlib 3.11+
    from packaging.requirements import Requirement
    assert Requirement("flask>=2.0").name == "flask"
