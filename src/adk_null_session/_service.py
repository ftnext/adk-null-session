"""A session service that never retains sessions between requests."""

from __future__ import annotations

from copy import deepcopy
import time
from typing import Any
import uuid

from google.adk.sessions.base_session_service import (
    BaseSessionService,
    GetSessionConfig,
    ListSessionsResponse,
)
from google.adk.sessions.session import Session


class NullSessionService(BaseSessionService):
    """Keep events and state only on each returned, independent Session.

    Use only for self-contained invocations. History-dependent continuation,
    including resume and human-in-the-loop flows, is unsupported.
    """

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Session:
        """Return an unsaved session, generating an ID if blank or missing."""
        resolved_id = session_id.strip() if session_id else ""
        return Session(
            id=resolved_id or str(uuid.uuid4()),
            app_name=app_name,
            user_id=user_id,
            state=deepcopy(state) if state is not None else {},
            events=[],
            last_update_time=time.time(),
        )

    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: GetSessionConfig | None = None,
    ) -> Session:
        """Return a fresh empty session, even for unknown or deleted IDs."""
        return Session(
            id=session_id,
            app_name=app_name,
            user_id=user_id,
            state={},
            events=[],
            last_update_time=time.time(),
        )

    async def list_sessions(
        self, *, app_name: str, user_id: str | None = None
    ) -> ListSessionsResponse:
        """Return an empty list because this service stores no sessions."""
        return ListSessionsResponse(sessions=[])

    async def delete_session(
        self, *, app_name: str, user_id: str, session_id: str
    ) -> None:
        """Do nothing because this service stores no sessions."""

    async def get_user_state(self, *, app_name: str, user_id: str) -> dict[str, Any]:
        """Return an empty dictionary; user state is never retained."""
        return {}

    # Inherit append_event: tools and agents need its invocation-local updates.
