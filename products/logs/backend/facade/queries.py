"""Facade re-export for the logs HogQL query runner.

Lets other products (e.g. dashboards widgets) run a logs query server-side without
reaching into logs internals — keeps the cross-product import at the facade boundary.
"""

from products.logs.backend.logs_query_runner import LogsQueryRunner

__all__ = ["LogsQueryRunner"]
