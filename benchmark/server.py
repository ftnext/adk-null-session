"""Isolated A2A receiver and its memory measurements (started by run.py)."""

from __future__ import annotations

import asyncio
import gc
import importlib.util
import logging
import os
import socket
import traceback
import tracemalloc
from pathlib import Path


def run_server(connection, service_name: str, sample_agent: str, delay_ms: float):
    """Spawn entry point; imports and application allocations stay in the child."""
    os.environ["OTEL_SDK_DISABLED"] = "true"
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(serve(connection, service_name, Path(sample_agent), delay_ms))
    except BaseException:
        try:
            connection.send({"error": traceback.format_exc()})
        finally:
            connection.close()
        raise


async def serve(connection, service_name, sample_agent, delay_ms):
    import psutil
    import uvicorn
    from a2a.server.apps import A2AStarletteApplication
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.tasks import InMemoryTaskStore
    from a2a.types import AgentCard
    from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.adk.runners import Runner
    from google.adk.sessions.in_memory_session_service import InMemorySessionService
    from google.genai import types

    from adk_null_session import NullSessionService

    class ScriptedPrimeLlm(BaseLlm):
        """Two fixed model steps; the sample's real function tool still executes."""

        model: str = "offline-prime-benchmark"
        delay_seconds: float = delay_ms / 1000

        async def generate_content_async(self, llm_request, stream=False):
            # Yield control so two or three receiver invocations actually overlap.
            await asyncio.sleep(self.delay_seconds)
            parts = llm_request.contents[-1].parts or []
            tool_results = [p.function_response for p in parts if p.function_response]
            if tool_results:
                result = tool_results[-1].response["result"]
                output = types.Part(text=result)
            else:
                output = types.Part(
                    function_call=types.FunctionCall(
                        id="prime-check", name="check_prime", args={"nums": [2, 17, 18]}
                    )
                )
            yield LlmResponse(content=types.Content(role="model", parts=[output]))

    spec = importlib.util.spec_from_file_location(
        "benchmark_prime_sample", sample_agent
    )
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot load sample: {sample_agent}")
    sample = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sample)
    agent = sample.root_agent
    agent.model = ScriptedPrimeLlm()
    expected = await sample.check_prime([2, 17, 18])
    card = AgentCard.model_validate_json(
        sample_agent.with_name("agent.json").read_text(encoding="utf-8")
    )

    # Start after imports. Warmup is included in the baseline, not the measured delta.
    tracemalloc.start()
    service = (
        InMemorySessionService()
        if service_name == "in_memory"
        else NullSessionService()
    )
    runner = Runner(app_name=agent.name, agent=agent, session_service=service)
    store = InMemoryTaskStore()
    handler = DefaultRequestHandler(
        agent_executor=A2aAgentExecutor(runner=runner), task_store=store
    )
    rpc_path = "/a2a/check_prime_agent"
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    card.url = f"http://127.0.0.1:{port}{rpc_path}"
    app = A2AStarletteApplication(agent_card=card, http_handler=handler).build(
        rpc_url=rpc_path
    )
    counters = {"active": 0, "max_active": 0, "completed": 0}

    class CountRequests:
        async def __call__(self, scope, receive, send):
            if scope["type"] != "http" or scope["path"] != rpc_path:
                return await app(scope, receive, send)
            counters["active"] += 1
            counters["max_active"] = max(counters["max_active"], counters["active"])
            try:
                await app(scope, receive, send)
                counters["completed"] += 1
            finally:
                counters["active"] -= 1

    server = uvicorn.Server(
        uvicorn.Config(
            CountRequests(),
            host="127.0.0.1",
            port=port,
            access_log=False,
            log_level="critical",
            lifespan="off",
            loop="asyncio",
            http="h11",
        )
    )
    server_task = asyncio.create_task(server.serve(sockets=[sock]))
    process = psutil.Process()
    try:
        while not server.started:
            if server_task.done():
                await server_task
                raise RuntimeError("A2A server exited before startup")
            await asyncio.sleep(0.01)
        connection.send({"url": card.url, "pid": os.getpid(), "expected": expected})
        while True:
            command = await asyncio.to_thread(connection.recv)
            if command == "stop":
                break
            if command not in {"baseline", "snapshot"}:
                raise ValueError(f"Unknown command: {command}")
            # The load generator drains each batch before asking for a snapshot.
            # Give response handlers a turn to finish releasing local references.
            await asyncio.sleep(0.02)
            if counters["active"]:
                raise RuntimeError("Memory snapshot requested with active requests")
            gc.collect()
            if command == "baseline":
                counters["max_active"] = 0
                counters["completed"] = 0
                tracemalloc.reset_peak()
            sessions = events = 0
            if isinstance(service, InMemorySessionService):
                for users in service.sessions.values():
                    for user_sessions in users.values():
                        sessions += len(user_sessions)
                        events += sum(len(s.events) for s in user_sessions.values())
            current, peak = tracemalloc.get_traced_memory()
            connection.send(
                {
                    "rss_bytes": process.memory_info().rss,
                    "traced_current_bytes": current,
                    "traced_peak_bytes": peak,
                    "sessions": sessions,
                    "events": events,
                    "tasks": len(store.tasks),
                    "completed": counters["completed"],
                    "max_active": counters["max_active"],
                }
            )
    finally:
        server.should_exit = True
        await server_task
        await runner.close()
        sock.close()
        connection.close()
