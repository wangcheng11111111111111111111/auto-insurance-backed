import pytest


@pytest.fixture(autouse=True)
def _enable_asyncio():
    pass


def pytest_configure(config):
    config.option.asyncio_mode = "auto"