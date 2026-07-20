from ecosystems.base import CertifyMode, ClosureMode, EcosystemProvider


def test_closure_mode_values():
    assert ClosureMode.LOCK.value == "lock"
    assert ClosureMode.RESOLVE.value == "resolve"
    assert ClosureMode.COMPUTE.value == "compute"


def test_certify_mode_values():
    assert CertifyMode.INSTALL.value == "install"
    assert CertifyMode.COMPILE.value == "compile"


def test_provider_protocol_surface():
    for method in ("detect", "closure_mode_for", "package_obligations", "native_obligations"):
        assert hasattr(EcosystemProvider, method)
