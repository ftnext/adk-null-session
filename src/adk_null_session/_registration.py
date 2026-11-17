"""Registration with ADK's process-local URI service registry."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._service import NullSessionService

if TYPE_CHECKING:
    from google.adk.cli.service_registry import ServiceRegistry


def _null_session_factory(uri: str, **kwargs: Any) -> NullSessionService:
    return NullSessionService()


def register_null_session_service(registry: ServiceRegistry | None = None) -> None:
    """Register ``null://`` in the supplied registry or ADK's global registry.

    Importing ``adk_null_session`` registers ``null://`` in ADK's global
    registry automatically. Call this function directly to register with a
    custom ``ServiceRegistry`` instance or re-register the global ``null``
    scheme.
    """
    if registry is None:
        from google.adk.cli.service_registry import get_service_registry

        registry = get_service_registry()
    registry.register_session_service("null", _null_session_factory)
