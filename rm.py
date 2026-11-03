import asyncio, json
from fastmcp import Client
from arcticdata_mcp.config import Settings
from arcticdata_mcp.server import create_server
from arcticdata_mcp.config import Settings
async def main():
    from arcticdata_mcp.client import DataONEClient
    s = Settings(); srv = create_server(s, require_auth=False)
    async with Client(srv) as c:
        r = (await c.call_tool("get_dataset_details", {"pid": "doi:10.18739/A2BG2HC7C", "fields": "id,formatType,resourceMap,obsoletedBy,obsoletes"})).structured_content
        print(json.dumps(r)[:500])
        r = (await c.call_tool("find_dataset_by_identifier", {"identifier": "resource_map_doi:10.18739/A2BG2HC7C"})).structured_content
        print(json.dumps(r)[:600])
asyncio.run(main())
