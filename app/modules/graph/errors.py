"""Safe, typed errors for the graph module.

Mirrors the rule already applied to `/readyz` in `app/dependencies/services.py`
and `app/api/health.py`: never let a driver exception's raw message reach a
caller, since some drivers embed connection strings or query text in it.
Every error here carries a short, safe, human-written message only.
"""

from __future__ import annotations


class GraphError(Exception):
    """Base class for all graph-module errors."""


class GraphConnectionError(GraphError):
    """A Neo4j read or write failed at the driver/transaction level.

    Raised with a fixed, safe message only -- never the underlying driver
    exception's text or the Cypher that was running, since a driver's error
    string can itself contain connection details.
    """


class GraphValidationError(GraphError):
    """A caller-supplied argument (case_id, a domain ID, pagination bounds) is invalid."""


class GraphNotFoundError(GraphError):
    """A requested case-scoped node does not exist."""
