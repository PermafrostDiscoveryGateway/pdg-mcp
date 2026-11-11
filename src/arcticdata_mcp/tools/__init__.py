"""MCP tool modules. Each module exposes ``register(mcp, ctx)``."""

from __future__ import annotations

from arcticdata_mcp import solr


def fields_in_use() -> solr.FieldUse:
    """Every index field name hard-coded in this package, for ``solr.check_fields``.

    get_dataset_details is not listed: it reads its field list from the index. Add new field constants here.
    """
    from arcticdata_mcp.tools import datasets, portals, search

    return solr.FieldUse(
        returned={
            *solr.METADATA_FIELDS, *solr.DATA_FIELDS, *search.EXTRA_RETURN_FIELDS,
            *datasets.PACKAGE_LOOKUP_FIELDS, *datasets.CITATION_FIELDS, *datasets.VERSION_FIELDS,
            *portals.PORTAL_FIELDS, *portals.DATASET_LOOKUP_FIELDS,
        },
        searched={
            *solr.QUERY_FIELDS, *search.QUERY_FIELDS, *datasets.QUERY_FIELDS,
            *portals.QUERY_FIELDS, *portals.SEARCH_FIELDS,
        },
        sorted_by=solr.sort_fields([*solr.SORT_OPTIONS.values(), *datasets.SORTS, *portals.SORTS]),
    )
