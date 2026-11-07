# adk-null-session

An invocation-local session service for [Google ADK](https://github.com/google/adk-python).
It returns independent `Session` objects without retaining them in the service.
Use it when every request supplies all the information needed to finish in a
single Runner invocation, including any ordinary function-call round trips.

Version **0.1.0** is an initial experimental package. It is not a transparent
replacement for a history-preserving session service.

## Install

Install from [PyPI](https://pypi.org/project/adk-null-session/) with Python 3.11 or newer:

```sh
python -m pip install adk-null-session
```

To install from a local checkout:

```sh
python -m pip install .
```

The runtime dependency is `google-adk>=1.27.1,<3`. This range targets both ADK
1.x and 2.x; it does not claim that every release or dependency combination has
been tested. The package does not directly require A2A SDK. Install the A2A
extras appropriate to your ADK version separately if your server uses A2A.

## Use with Runner

This example runs locally without model credentials or network calls:

```python
import asyncio

from google.adk.agents.base_agent import BaseAgent
from google.adk.events.event import Event
from google.adk.runners import Runner
from google.genai import types

from adk_null_session import NullSessionService


class EchoAgent(BaseAgent):
    async def _run_async_impl(self, ctx):
        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(
                role="model",
                parts=[types.Part(text=ctx.session.state["request_text"])],
            ),
        )


async def main():
    runner = Runner(
        app_name="example",
        agent=EchoAgent(name="echo"),
        session_service=NullSessionService(),
    )
    async for event in runner.run_async(
        user_id="user",
        session_id="request-1",
        new_message=types.Content(
            role="user", parts=[types.Part(text="Hello")]
        ),
        state_delta={"request_text": "Hello"},
    ):
        if event.content:
            for part in event.content.parts or []:
                if part.text:
                    print(part.text)


asyncio.run(main())
```

`create_session()` is optional for this service: `get_session()` accepts any ID.
If you call `create_session(state=...)`, that state exists only in its returned
object. Runner retrieves a new empty object through `get_session()`, so the
initial state is lost **before the first invocation**. Pass per-request values
through `run_async(state_delta=...)`, as above, or include them in the request.

## Explicit URI registration

```python
from google.adk.cli.service_registry import get_service_registry
from adk_null_session import NullSessionService, register_null_session_service

register_null_session_service()
service = get_service_registry().create_session_service("null://")
assert isinstance(service, NullSessionService)
```

Call registration in the same Python process before building your ADK server
and selecting `null://` as its session service URI. You can also pass an explicit
`ServiceRegistry` instance to `register_null_session_service(registry)`.
The factory ignores URI contents and keyword arguments. Registration replaces
any existing `null` factory in that registry. Installing or importing the
package does **not** register a URI scheme, start uvicorn, or configure logging.
Running an unrelated `adk web --session_service_uri null://` process after
installation alone will not perform this registration.

## Behavior

| API | Result |
| --- | --- |
| `create_session(...)` | New unsaved session; trims a supplied ID or generates a UUID for a missing/blank ID. Copies supplied initial state. |
| `get_session(...)` | New, empty, independent session with exactly the requested ID on every call; event filters have no stored events to filter. |
| `list_sessions(...)` | Empty `ListSessionsResponse`. |
| `delete_session(...)` | No operation. A later lookup still returns an empty session. |
| `get_user_state(...)` | New empty dictionary, including on ADK versions predating the base API. |
| `append_event(session, event)` | Inherited ADK behavior: updates the supplied session's events and state during the invocation; partial events are not committed. |

The service holds no session references. Ordinary function calling, tool state,
and agent `output_key` values remain available within a single invocation.
`app:` and `user:` state are also invocation-local; neither is shared across
requests. Duplicate IDs are accepted. Unknown and deleted IDs look like empty
sessions, so this service cannot support existence checks, session browsing,
or tools such as Web UI history/evaluation that require stored sessions.

The inherited `append_event` does not advance `last_update_time` in the tested
ADK releases. The timestamp records construction time, not the latest event.
Events and state still occupy memory and can grow during an invocation.

## Limitations that affect correctness

- **No multi-turn memory.** Each request must carry the full information it
  needs. `RemoteA2aAgent` can reuse a returned `context_id` without sending earlier
  history. Even `full_history_when_stateless=True` does not force full history
  when a context ID exists in the tested releases. Replacing an A2A server's
  session store can therefore silently remove context needed by later turns.
- **No continuation.** Human-in-the-loop approval, additional authentication,
  follow-up results for `LongRunningFunctionTool`, resume, and rewind require
  history this service discards. In tested ADK 1.x, resuming by `invocation_id`
  alone raises a missing-history error. In ADK 2.13.0, an `LlmAgent` can instead
  invoke its model again; a scripted repeated tool request reproduced duplicate
  execution (the in-memory control executed once). Do not use this service for
  these flows. The service cannot reliably detect resume from `get_session`
  arguments, which contain no invocation ID, and does not provide a resume guard.
- **No server-wide retention or memory guarantee.** A2A's `InMemoryTaskStore` is
  separate. In a ten-request probe, all ten completed Tasks still held history
  and artifacts. Other stores, logs, traces, caller references, and active
  invocations can retain data independently. Configure their policies separately.

## Compatibility and verification

The minimum is ADK **1.27.1** because
[1.27.0 was yanked on PyPI](https://pypi.org/project/google-adk/1.27.0/). In 1.25.0,
base `append_event` fails to pass `temp:output_key` to a later agent; the same
failure occurs with `InMemorySessionService`. It was fixed by upstream commit
[`2780ae2`](https://github.com/google/adk-python/commit/2780ae2892adfbebc7580c843d2eaad29f86c335)
and the probe passes on 1.27.0, 1.39.1, and 2.13.0. Python >=3.11 is chosen to
cover the minimum required by the tested ADK 2.x release.

The [original implementation](https://gist.github.com/ftnext/8803f16f0c29f0f2436f54d87dd295d9/53972c63966affa41ff6e91326207f9068849a94)
was reviewed against source tags 1.25.0, 1.27.0, 1.39.1, and 2.13.0: **89 passed,
3 skipped** over 23 cases per tag. The probes include assertions of unsupported
behavior, so passing does not certify suitability for those flows. This package
adds an explicit empty `get_user_state` implementation and copies initial state.
Its tests preserve the behavior probes and add checks for the package API.
The installed 0.1.0 wheel passed **31 tests per tag (93 total)** on 1.27.0,
1.39.1, and 2.13.0; the Runner example above also ran successfully on each tag.

The original and package source-tag checks use Python **3.13.18**, a shared
environment with **a2a-sdk 0.3.25**, **google-genai 2.20.0**, and **pydantic 2.13.4**.
They exercise real Runner/tool/A2A handler code with scripted model responses,
20 concurrent invocations, and session-reference release. This is a source
behavior check, not a resolved dependency matrix: for example, that shared
`google-genai` version falls outside the declared ranges of ADK 1.27.0 and
2.13.0. No external LLM, HTTP transport, full dependency matrix, other Python
version, or production load test was exercised.

## Development

The project follows
[`cookiecutter-develop-pypackage` 0.4.0](https://github.com/ftnext/cookiecutter-develop-pypackage/tree/0.4.0),
with `project_dir_name=adk-null-session`, `package_name=adk_null_session`, and
`use_taskipy=no`. It uses setuptools and reads the package version from
`adk_null_session.__version__`. The existing Apache-2.0 license and service
implementation are retained; no command-line application is provided.

Create an isolated environment and run the same checks used in CI:

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e '.[dev]'
ruff check .
python -m pytest -v
python -m build
python -m twine check --strict dist/*
```

The `dev` extra includes pytest-randomly, async/A2A test dependencies, Ruff,
build, and twine. The smaller `test` and `test-a2a` extras remain available.
CI runs on pushes and pull requests using Python 3.11–3.14 with freshly resolved
dependencies. A separate Python 3.13 job tests ADK 1.27.1 without the A2A SDK.
A green CI run checks those resolved environments; it does not
establish compatibility with every ADK release in the declared dependency range.

In addition to the earlier shared-environment probes, template integration was
verified in a fresh Python 3.13.18 environment with installed **google-adk 2.13.0**,
**google-genai 2.34.0**, **pydantic 2.14.2**, and **a2a-sdk 0.3.26**: all **31 tests
passed**. Other Python versions in the CI matrix have not been run locally.

A2A-specific tests skip when A2A SDK is unavailable. Tests make no external model
calls. Build artifacts include the implementation and tests, not exported ADK
source trees. The public API is `NullSessionService` and
`register_null_session_service` from `adk_null_session`.

Based on the [author's article](https://nikkie-ftnext.hatenablog.com/entry/google-adk-python-a2a-server-idea-null-session-service).
Licensed under the existing [Apache License 2.0](LICENSE).


## Releasing

Update `__version__` in `src/adk_null_session/__init__.py` and the version stated
in this README, then run the development checks and review the built wheel and
source distribution. Once CI passes for the release commit, publish a GitHub
Release with a tag matching the package version (`vX.Y.Z`). The
[publish workflow](.github/workflows/publish.yml) builds and checks the
distributions, then uploads them to PyPI.
