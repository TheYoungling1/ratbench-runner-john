def pytest_configure(config):
    config.addinivalue_line(
        "markers", "slow: end-to-end tests that build real Docker images"
    )
