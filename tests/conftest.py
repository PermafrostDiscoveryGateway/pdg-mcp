import pytest
import respx
from fastmcp import Client

from arcticdata_mcp.config import Settings
from arcticdata_mcp.server import create_server

MN = "https://arcticdata.io/metacat/d1/mn/v2"


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.downloads.max_inline_bytes = 1000
    s.downloads.download_dir = str(tmp_path / "dl")
    return s


@pytest.fixture
def mock_api():
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
async def client(settings, mock_api):
    async with Client(create_server(settings, require_auth=False)) as c:
        yield c
