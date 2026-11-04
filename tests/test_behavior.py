"""Offline behavioral probes; assertions describe observed behavior, not a safety certification.

Run against an installed ADK release, or git-exported sources via PYTHONPATH. LLM outputs are scripted; no network or model credentials are used.
"""
import asyncio
import copy
import gc
import json
import weakref

import pytest
from pydantic import PrivateAttr
from google.genai import types
from google.adk import __version__
from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.llm_agent import LlmAgent
from google.adk.agents.sequential_agent import SequentialAgent
from google.adk.apps.app import App, ResumabilityConfig
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.tools.tool_context import ToolContext

from adk_null_session import NullSessionService, register_null_session_service

pytestmark = pytest.mark.asyncio
IDS = dict(app_name='probe', user_id='u', session_id='s')


def content(text):
    return types.Content(role='user', parts=[types.Part(text=text)])


class Probe(BaseAgent):
    async def _run_async_impl(self, ctx):
        await asyncio.sleep(0)
        payload = {
            'state': copy.deepcopy(ctx.session.state),
            'messages': [p.text for e in ctx.session.events if e.content
                         for p in e.content.parts or [] if p.text],
        }
        yield Event(author=self.name, invocation_id=ctx.invocation_id, content=types.Content(
            role='model', parts=[types.Part(text=json.dumps(payload))]))


class ScriptedLlm(BaseLlm):
    model: str = 'offline-probe'
    _requests: list = PrivateAttr(default_factory=list)
    _responses: list = PrivateAttr(default_factory=list)

    async def generate_content_async(self, llm_request, stream=False):
        self._requests.append(copy.deepcopy(llm_request))
        assert self._responses, 'Unexpected extra model invocation'
        yield self._responses.pop(0).model_copy(deep=True)


def response(*parts):
    return LlmResponse(content=types.Content(role='model', parts=list(parts)))


async def run(runner, text='hello', **kwargs):
    return [event async for event in runner.run_async(
        user_id='u', session_id='s', new_message=content(text), **kwargs)]


def payload(events):
    return json.loads(next(e.content.parts[0].text for e in reversed(events)
                          if e.content and e.content.parts and e.content.parts[0].text))


async def test_append_preserves_local_events_and_state():
    service = NullSessionService()
    session = await service.get_session(**IDS)
    event = Event(author='probe', actions=EventActions(state_delta={
        'x': 1, 'app:shared': 2, 'user:pref': 3}))
    await service.append_event(session, event)
    assert session.events == [event]
    assert session.state == {'x': 1, 'app:shared': 2, 'user:pref': 3}
    assert (await service.get_session(**IDS)).state == {}
    assert (await service.get_session(**IDS)).events == []


async def test_partial_event_not_committed():
    service = NullSessionService()
    session = await service.get_session(**IDS)
    await service.append_event(session, Event(author='probe', partial=True,
        actions=EventActions(state_delta={'x': 1})))
    assert session.events == [] and session.state == {}


@pytest.mark.parametrize('service_type, expected', [
    (NullSessionService, {}), (InMemorySessionService, {'seed': 'present'}),
])
async def test_create_state_at_first_runner_invocation(service_type, expected):
    service = service_type()
    await service.create_session(**IDS, state={'seed': 'present'})
    runner = Runner(app_name='probe', agent=Probe(name='probe'), session_service=service)
    assert payload(await run(runner))['state'] == expected


async def test_state_delta_reaches_first_invocation():
    runner = Runner(app_name='probe', agent=Probe(name='probe'),
                    session_service=NullSessionService())
    state = {'seed': 'present', 'app:x': 2, 'user:x': 3}
    assert payload(await run(runner, state_delta=state))['state'] == state


@pytest.mark.parametrize('service_type, has_previous', [
    (NullSessionService, False), (InMemorySessionService, True),
])
async def test_multi_turn_history(service_type, has_previous):
    service = service_type()
    await service.create_session(**IDS)
    runner = Runner(app_name='probe', agent=Probe(name='probe'), session_service=service)
    await run(runner, 'first')
    actual = payload(await run(runner, 'second'))['messages']
    assert ('first' in actual) is has_previous
    assert 'second' in actual


async def test_real_function_call_roundtrip_and_sequential_state():
    model = ScriptedLlm()
    model._responses = [
        response(types.Part(function_call=types.FunctionCall(
            id='fc1', name='calculate', args={'n': 7}))),
        response(types.Part(text='49')),
    ]

    def calculate(n: int, tool_context: ToolContext) -> int:
        tool_context.state['tool_value'] = n * n
        return n * n

    agent = SequentialAgent(name='sequence', sub_agents=[
        LlmAgent(name='calculator', model=model, tools=[calculate], output_key='answer'),
        Probe(name='probe'),
    ])
    runner = Runner(app_name='probe', agent=agent, session_service=NullSessionService())
    result = payload(await run(runner, 'square 7'))
    assert result['state']['tool_value'] == 49
    assert result['state']['answer'] == '49'
    parts = [p for c in model._requests[1].contents for p in c.parts or []]
    assert any(p.function_call and p.function_call.name == 'calculate' for p in parts)
    assert any(p.function_response and p.function_response.name == 'calculate' for p in parts)


@pytest.mark.parametrize('service_type', [NullSessionService, InMemorySessionService])
async def test_temp_output_key_version_behavior(service_type):
    model = ScriptedLlm()
    model._responses = [response(types.Part(text='ephemeral'))]
    agent = SequentialAgent(name='sequence', sub_agents=[
        LlmAgent(name='writer', model=model, output_key='temp:answer'),
        Probe(name='reader'),
    ])
    service = service_type()
    await service.create_session(**IDS)
    runner = Runner(app_name='probe', agent=agent, session_service=service)
    actual = payload(await run(runner))['state'].get('temp:answer')
    print('TEMP_OUTPUT', __version__, repr(actual))
    # Observational assertion deliberately exposes the older base implementation.
    assert actual == (None if __version__ == '1.25.0' else 'ephemeral')


async def test_parallel_same_id_keeps_invocations_separate():
    runner = Runner(app_name='probe', agent=Probe(name='probe'),
                    session_service=NullSessionService())
    results = await asyncio.gather(*(run(runner, str(i), state_delta={'i': i}) for i in range(20)))
    for i, events in enumerate(results):
        observed = payload(events)
        assert observed['state']['i'] == i
        assert observed['messages'] == [str(i)]


async def test_unknown_and_deleted_sessions_still_exist():
    service = NullSessionService()
    assert (await service.get_session(**IDS)).id == 's'
    await service.delete_session(**IDS)
    assert (await service.get_session(**IDS)).id == 's'
    assert (await service.list_sessions(app_name='probe')).sessions == []


async def test_last_update_time_not_updated_by_base_append():
    service = NullSessionService()
    session = await service.get_session(**IDS)
    before = session.last_update_time
    await service.append_event(session, Event(author='probe', timestamp=before + 100))
    assert session.last_update_time == before


async def test_no_retained_session_reference():
    service = NullSessionService()
    refs = []
    for _ in range(100):
        session = await service.get_session(**IDS)
        await service.append_event(session, Event(author='probe', content=content('x' * 1000)))
        refs.append(weakref.ref(session))
    del session
    gc.collect()
    assert not any(ref() for ref in refs)


async def test_resume_cannot_recover_previous_invocation():
    app = App(name='probe', root_agent=Probe(name='probe'),
              resumability_config=ResumabilityConfig(is_resumable=True))
    runner = Runner(app=app, session_service=NullSessionService())
    events = await run(runner)
    invocation_id = events[-1].invocation_id
    assert invocation_id
    with pytest.raises(ValueError, match='[Nn]o events to resume|[Nn]o user message'):
        _ = [e async for e in runner.run_async(user_id='u', session_id='s',
                                              invocation_id=invocation_id)]


async def test_rewind_cannot_find_previous_invocation():
    runner = Runner(app_name='probe', agent=Probe(name='probe'),
                    session_service=NullSessionService())
    events = await run(runner)
    with pytest.raises(ValueError, match='[Nn]ot found'):
        await runner.rewind_async(user_id='u', session_id='s',
                                  rewind_before_invocation_id=events[-1].invocation_id)


async def test_registered_uri_creates_service():
    from google.adk.cli.service_registry import ServiceRegistry
    registry = ServiceRegistry()
    assert registry.create_session_service('null://') is None
    register_null_session_service(registry)
    assert isinstance(registry.create_session_service('null://', arbitrary='value'), NullSessionService)


async def test_a2a_completed_tasks_remain_even_without_adk_sessions():
    pytest.importorskip('a2a')
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.tasks import InMemoryTaskStore
    from a2a.types import Message, MessageSendParams, Part, TextPart, TaskState
    from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor

    service = NullSessionService()
    runner = Runner(app_name='probe', agent=Probe(name='probe'), session_service=service)
    store = InMemoryTaskStore()
    handler = DefaultRequestHandler(agent_executor=A2aAgentExecutor(runner=runner), task_store=store)
    results = []
    for i in range(10):
        result = await handler.on_message_send(MessageSendParams(message=Message(
            message_id=f'm{i}', context_id='same-context', role='user',
            parts=[Part(root=TextPart(text=f'input-{i}'))])))
        assert result.status.state == TaskState.completed
        results.append(result)
    assert len(store.tasks) == 10
    assert results[0].history
    assert results[0].artifacts
    assert (await service.list_sessions(app_name='probe')).sessions == []
    text = results[-1].artifacts[0].parts[0].root.text
    assert json.loads(text)['messages'] == ['input-9']


async def test_get_user_state_contract_if_present():
    service = NullSessionService()
    assert await service.get_user_state(app_name='probe', user_id='u') == {}


@pytest.mark.parametrize('full_history_when_stateless', [False, True])
async def test_remote_a2a_client_reuses_context_and_omits_old_history(full_history_when_stateless):
    pytest.importorskip('a2a')
    from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
    agent = RemoteA2aAgent(name='remote', agent_card='https://example.invalid/card',
                           full_history_when_stateless=full_history_when_stateless)
    service = NullSessionService()
    session = await service.get_session(**IDS)
    session.events = [
        Event(author='user', content=content('Remember the number 7')),
        Event(author='remote', content=types.Content(role='model', parts=[types.Part(text='OK')]),
              custom_metadata={'a2a:response': True, 'a2a:context_id': 'remote-context'}),
        Event(author='user', content=content('Square that number')),
    ]
    runner = Runner(app_name='probe', agent=agent, session_service=service)
    ctx = runner._new_invocation_context(session=session, new_message=content('Square that number'))
    parts, context_id = agent._construct_message_parts_from_session(ctx)
    assert context_id == 'remote-context'
    assert [p.root.text for p in parts] == ['Square that number']


@pytest.mark.parametrize('service_type', [NullSessionService, InMemorySessionService])
async def test_long_running_tool_cannot_resume_after_interrupt(service_type):
    from google.adk.tools.long_running_tool import LongRunningFunctionTool

    calls = []
    def wait_for_approval() -> None:
        calls.append('started')
        return None

    model = ScriptedLlm()
    waiting_response = response(types.Part(function_call=types.FunctionCall(
        id='approval-1', name='wait_for_approval', args={})))
    model._responses = [waiting_response, waiting_response]
    agent = LlmAgent(name='approver', model=model,
                     tools=[LongRunningFunctionTool(wait_for_approval)])
    app = App(name='probe', root_agent=agent,
              resumability_config=ResumabilityConfig(is_resumable=True))
    service = service_type()
    await service.create_session(**IDS)
    runner = Runner(app=app, session_service=service)
    events = await run(runner, 'request approval')
    waiting = next(e for e in events if e.long_running_tool_ids)
    assert waiting.invocation_id
    assert calls == ['started']
    if service_type is InMemorySessionService:
        resumed = [e async for e in runner.run_async(user_id='u', session_id='s',
            invocation_id=waiting.invocation_id)]
        assert calls == ['started']
    elif __version__.startswith('1.'):
        with pytest.raises(ValueError, match='[Nn]o events to resume|[Nn]o user message'):
            _ = [e async for e in runner.run_async(user_id='u', session_id='s',
                invocation_id=waiting.invocation_id)]
    else:
        resumed = [e async for e in runner.run_async(user_id='u', session_id='s',
            invocation_id=waiting.invocation_id)]
        assert any(e.long_running_tool_ids for e in resumed)
        assert calls == ['started', 'started']
        assert len(model._requests) == 2
        assert not any(p.text == 'request approval'
                       for c in model._requests[-1].contents for p in c.parts or [])
