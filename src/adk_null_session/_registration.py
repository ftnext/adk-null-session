"""Explicit registration with ADK's process-local URI service registry."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from ._service import NullSessionService

if TYPE_CHECKING:
    from google.adk.cli.service_registry import ServiceRegistry


def _null_session_factory(uri: str, **kwargs: Any) -> NullSessionService:
    return NullSessionService()


def register_null_session_service(registry: ServiceRegistry | None = None) -> None:
    """Register ``null://`` in the supplied registry or ADK's global registry.

    Call explicitly in the same process before constructing the ADK server.
    Importing or installing this package does not register a scheme.
    """
    if registry is None:
        from google.adk.cli.service_registry import get_service_registry

        registry = get_service_registry()
    registry.register_session_service("null", _null_session_factory)
