"""Compare receiver memory using real loopback A2A JSON-RPC requests."""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import importlib.metadata
import json
import multiprocessing
import platform
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from server import run_server

SAMPLE = (
    Path.home()
    / "study/reading/adk-python/contributing/samples/a2a/a2a_basic"
    / "remote_a2a/check_prime_agent/agent.py"
)
REQUEST_TEXT = "Check whether 2, 17 and 18 are prime."
METRICS = ("rss_bytes", "traced_current_bytes", "sessions", "events", "tasks")


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=positive_int, default=1000)
    parser.add_argument("--concurrency", type=int, choices=(2, 3), default=3)
    parser.add_argument("--repeats", type=positive_int, default=3)
    parser.add_argument("--warmup", type=positive_int, default=30)
    parser.add_argument("--sample-every", type=positive_int, default=100)
    parser.add_argument("--sample-agent", type=Path, default=SAMPLE)
    parser.add_argument("--output", type=Path, help="new output directory")
    args = parser.parse_args()
    if args.requests < args.concurrency or args.sample_every < args.concurrency:
        parser.error("--requests and --sample-every must be >= --concurrency")
    args.sample_agent = args.sample_agent.expanduser().resolve()
    for path in (args.sample_agent, args.sample_agent.with_name("agent.json")):
        if not path.is_file():
            parser.error(f"sample file does not exist: {path}; use --sample-agent")
    return args


def receive(connection, timeout=60):
    if not connection.poll(timeout):
        raise TimeoutError("Timed out waiting for the receiver process")
    value = connection.recv()
    if "error" in value:
        raise RuntimeError(value["error"])
    return value


async def send_batch(client, url, expected, start, count, concurrency, phase):
    indices = iter(range(start, start + count))

    async def worker():
        for index in indices:
            request_id = f"{phase}-{index}"
            response = await client.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "message/send",
                    "params": {
                        "message": {
                            "messageId": request_id,
                            "role": "user",
                            "parts": [{"kind": "text", "text": REQUEST_TEXT}],
                            # No contextId/taskId: each request is an independent invocation.
                        }
                    },
                },
            )
            response.raise_for_status()
            body = response.json()
            if body.get("id") != request_id or "error" in body:
                raise RuntimeError(f"A2A request {request_id} failed: {body}")
            task = body.get("result", {})
            texts = [
                part.get("text")
                for artifact in task.get("artifacts", [])
                for part in artifact.get("parts", [])
                if part.get("kind") == "text"
            ]
            if task.get("status", {}).get("state") != "completed" or texts != [
                expected
            ]:
                raise RuntimeError(f"Unexpected task for {request_id}: {task}")
            # No response bodies or Task objects are accumulated by the client.

    async with asyncio.TaskGroup() as group:
        for _ in range(concurrency):
            group.create_task(worker())


def snapshot(connection, service, repeat, completed, elapsed, baseline=None):
    connection.send("baseline" if baseline is None else "snapshot")
    row = receive(connection)
    row.update(
        service=service, repeat=repeat, requests=completed, elapsed_seconds=elapsed
    )
    reference = baseline if baseline is not None else row
    for metric in METRICS:
        row[f"delta_{metric}"] = row[metric] - reference[metric]
    if row["completed"] != completed:
        raise RuntimeError(f"Receiver completion count mismatch: {row}")
    expected_sessions = completed if service == "in_memory" else 0
    if row["delta_sessions"] != expected_sessions or row["delta_tasks"] != completed:
        raise RuntimeError(f"Unexpected store retention counts: {row}")
    return row


async def run_case(args, service, repeat, writer, csv_file):
    context = multiprocessing.get_context("spawn")
    connection, child_connection = context.Pipe()
    process = context.Process(
        target=run_server,
        args=(child_connection, service, str(args.sample_agent), 5.0),
    )
    process.start()
    child_connection.close()
    succeeded = False
    try:
        ready = receive(connection)
        async with httpx.AsyncClient(
            timeout=60,
            trust_env=False,
            limits=httpx.Limits(
                max_connections=args.concurrency,
                max_keepalive_connections=args.concurrency,
            ),
        ) as client:
            await send_batch(
                client,
                ready["url"],
                ready["expected"],
                0,
                args.warmup,
                args.concurrency,
                "warmup",
            )
            baseline = snapshot(connection, service, repeat, 0, 0)
            rows = [baseline]
            writer.writerow(baseline)
            csv_file.flush()
            elapsed = 0.0
            for start in range(0, args.requests, args.sample_every):
                count = min(args.sample_every, args.requests - start)
                began = time.perf_counter()
                await send_batch(
                    client,
                    ready["url"],
                    ready["expected"],
                    start,
                    count,
                    args.concurrency,
                    "measured",
                )
                elapsed += time.perf_counter() - began
                row = snapshot(
                    connection, service, repeat, start + count, elapsed, baseline
                )
                rows.append(row)
                writer.writerow(row)
                csv_file.flush()
                mib = 1024**2
                print(
                    f"{service:9} repeat={repeat} requests={start + count:5} "
                    f"RSS Δ={row['delta_rss_bytes'] / mib:7.2f} MiB "
                    f"traced Δ={row['delta_traced_current_bytes'] / mib:7.2f} MiB "
                    f"max_active={row['max_active']}",
                    flush=True,
                )
            if not 2 <= rows[-1]["max_active"] <= args.concurrency:
                raise RuntimeError("Receiver did not observe the requested concurrency")
            succeeded = True
            return {
                "service": service,
                "repeat": repeat,
                "pid": ready["pid"],
                "expected_response": ready["expected"],
                "baseline": baseline,
                "final": rows[-1],
            }
    finally:
        if process.is_alive():
            try:
                connection.send("stop")
            except (BrokenPipeError, OSError):
                pass
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join()
        connection.close()
        if succeeded and process.exitcode != 0:
            raise RuntimeError(f"Receiver exited with code {process.exitcode}")


async def main(args):
    output = args.output or Path(__file__).parent / "results" / datetime.now().strftime(
        "%Y%m%d-%H%M%S-%f"
    )
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version,
        "platform": platform.platform(),
        "versions": {
            p: importlib.metadata.version(p)
            for p in (
                "adk-null-session",
                "google-adk",
                "a2a-sdk",
                "google-genai",
                "pydantic",
                "httpx",
                "uvicorn",
                "psutil",
            )
        },
        "config": {
            k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()
        },
        "sample_sha256": hashlib.sha256(args.sample_agent.read_bytes()).hexdigest(),
        "request_text": REQUEST_TEXT,
        "model": "scripted; 5 ms delay per model step",
        "task_store": "InMemoryTaskStore (retained in both conditions)",
        "otel_sdk_disabled": True,
        "runs": [],
    }
    report_file = output / "report.json"

    def save():
        report_file.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    save()
    fields = [
        "service",
        "repeat",
        "requests",
        "elapsed_seconds",
        "rss_bytes",
        "traced_current_bytes",
        "traced_peak_bytes",
        "sessions",
        "events",
        "tasks",
        "completed",
        "max_active",
        *[f"delta_{m}" for m in METRICS],
    ]
    print(f"Results: {output}", flush=True)
    try:
        with (output / "samples.csv").open(
            "w", newline="", encoding="utf-8"
        ) as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fields)
            writer.writeheader()
            for repeat in range(1, args.repeats + 1):
                # Alternate order to reduce systematic ordering effects.
                services = (
                    ("in_memory", "null") if repeat % 2 else ("null", "in_memory")
                )
                for service in services:
                    report["runs"].append(
                        await run_case(args, service, repeat, writer, csv_file)
                    )
                    save()
        report["median_final_deltas"] = {
            service: {
                metric: statistics.median(
                    run["final"][f"delta_{metric}"]
                    for run in report["runs"]
                    if run["service"] == service
                )
                for metric in METRICS
            }
            for service in ("in_memory", "null")
        }
        report["status"] = "completed"
    except BaseException as error:
        report.update(status="failed", error=repr(error))
        raise
    finally:
        save()
    print(f"Report: {report_file}")


if __name__ == "__main__":
    asyncio.run(main(arguments()))
