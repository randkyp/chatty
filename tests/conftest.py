import pytest

from chatty.api import close_clients


@pytest.fixture(autouse=True)
def cleanup_clients():
    """Ensure httpx clients are closed and the cache is cleared between tests.
    This prevents `respx_mock` from failing when a test reuses a client created
    in a different mock context.
    """
    yield
    close_clients()
