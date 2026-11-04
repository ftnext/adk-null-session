"""Public package contract beyond the original article probes."""
import uuid

import pytest
from google.adk.cli.service_registry import get_service_registry
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions

from adk_null_session import NullSessionService, register_null_session_service

IDS = dict(app_name="test", user_id="u", session_id="s")


@pytest.mark.parametrize("session_id", [None, "", "  "])
async def test_blank_ids_are_generated(session_id):
    session = await NullSessionService().create_session(**(IDS | {"session_id": session_id}))
    assert str(uuid.UUID(session.id)) == session.id


async def test_created_state_is_independent_and_not_retrievable():
    service = NullSessionService()
    state = {"nested": {"value": 1}}
    created = await service.create_session(**IDS, state=state)
    state["nested"]["value"] = 2
    assert created.state == {"nested": {"value": 1}}
    assert (await service.get_session(**IDS)).state == {}
    assert (await service.create_session(**(IDS | {"session_id": " s "}))).id == "s"


async def test_get_sessions_and_user_state_are_independent():
    service = NullSessionService()
    first = await service.get_session(**IDS)
    first.state["x"] = 1
    first.events.append(Event(author="test"))
    second = await service.get_session(**IDS)
    assert second is not first
    assert second.state == {} and second.events == []
    user_state = await service.get_user_state(app_name="test", user_id="u")
    user_state["x"] = 1
    assert await service.get_user_state(app_name="test", user_id="u") == {}


async def test_partial_event_does_not_change_timestamp_or_state():
    service = NullSessionService()
    session = await service.get_session(**IDS)
    before = session.last_update_time
    await service.append_event(session, Event(author="test", partial=True,
        timestamp=before + 1, actions=EventActions(state_delta={"x": 1})))
    assert session.last_update_time == before
    assert session.state == {} and session.events == []


def test_global_registration_is_explicit_and_creates_independent_services():
    register_null_session_service()
    registry = get_service_registry()
    first = registry.create_session_service("null://")
    second = registry.create_session_service("null://", app_name="ignored")
    assert isinstance(first, NullSessionService)
    assert isinstance(second, NullSessionService)
    assert first is not second


def test_import_does_not_register_or_configure_logging():
    import subprocess
    import sys

    subprocess.run([sys.executable, "-c", '''
import logging
from google.adk.cli.service_registry import get_service_registry
registry = get_service_registry()
assert registry.create_session_service("null://") is None
before = (logging.root.level, list(logging.root.handlers))
import adk_null_session
assert registry.create_session_service("null://") is None
assert (logging.root.level, list(logging.root.handlers)) == before
'''], check=True)
