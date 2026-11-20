import asyncio

from fastmcp import Client


async def call(c, name, args):
    try:
        await c.call_tool(name, args)
        return "ok"
    except Exception as e:
        return type(e).__name__


async def main():
    async with Client("http://127.0.0.1:8000/mcp") as c:
        for i in range(5):
            await call(c, "search_datasets", {"query": f"permafrost {i}", "rows": 0})  # 5 ok
        await call(c, "get_facet_counts", {"field": "formatId"})  # 1 ok
        await call(c, "made_up_tool", {})  # not_found
        await call(c, "search_datasets", {"query": "x", "rows": -1})  # invalid_arguments
        await call(c, "search_datasets", {"query": "my-secret-search-term", "rows": 0})  # ok; must never appear in logs
        await asyncio.sleep(12)  # let the rate-limit bucket refill
        results = await asyncio.gather(*[call(c, "search_datasets", {"query": "burst", "rows": 0}) for _ in range(40)])
        print("burst:", {k: results.count(k) for k in sorted(set(results))})


asyncio.run(main())
