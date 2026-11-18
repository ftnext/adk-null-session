"""Invocation-local session handling for Google ADK."""

__version__ = "0.2.0"

from ._registration import register_null_session_service
from ._service import NullSessionService

__all__ = ["NullSessionService", "register_null_session_service"]

register_null_session_service()
