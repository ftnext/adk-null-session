"""Compare receiver memory using real loopback A2A JSON-RPC requests."""

from __future__ import annotations

import argparse
import asyncio
import csv
import importlib.metadata
import json
import multiprocessing
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .dataset import SCHEMA_VERSION, source_info, summarize
from .measurement import MODES, metrics_for
from .server import run_server

ROOT = Path(__file__).resolve().parent
SAMPLE = (
    ROOT
    / ".samples/adk-python/contributing/samples/a2a/a2a_basic/remote_a2a/check_prime_agent/agent.py"
)
REQUEST_TEXT = "Check whether 2, 17 and 18 are prime."
METRICS = tuple(dict.fromkeys(metric for mode in MODES for metric in metrics_for(mode)))


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=positive_int, default=1000)
    parser.add_argument("--concurrency", type=positive_int, default=3)
    parser.add_argument("--repeats", type=positive_int, default=3)
    parser.add_argument("--warmup", type=positive_int, default=30)
    parser.add_argument("--sample-every", type=positive_int, default=100)
    parser.add_argument("--measurement", choices=("both", *MODES), default="both")
    parser.add_argument("--sample-agent", type=Path, default=SAMPLE)
    parser.add_argument("--output", type=Path, help="new output directory")
    args = parser.parse_args(argv)
    args.measurement_modes = (
        list(MODES) if args.measurement == "both" else [args.measurement]
    )
    if args.requests < args.concurrency or args.sample_every < args.concurrency:
        parser.error("--requests and --sample-every must be >= --concurrency")
    args.sample_agent = args.sample_agent.expanduser().resolve()
    for path in (args.sample_agent, args.sample_agent.with_name("agent.json")):
        if not path.is_file():
            parser.error(
                f"sample file does not exist: {path}; follow benchmark/README.md or use --sample-agent"
            )
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


def snapshot(
    connection,
    service,
    mode,
    repeat,
    completed,
    batch_count,
    concurrency,
    elapsed,
    baseline=None,
):
    connection.send("baseline" if baseline is None else "snapshot")
    row = receive(connection)
    row.update(
        service=service,
        measurement_mode=mode,
        repeat=repeat,
        requests=completed,
        elapsed_seconds=elapsed,
    )
    reference = baseline if baseline is not None else row
    for metric in metrics_for(mode):
        row[f"delta_{metric}"] = row[metric] - reference[metric]
    if row["completed"] != completed or row["batch_completed"] != batch_count:
        raise RuntimeError(f"Receiver completion count mismatch: {row}")
    if row["batch_max_active"] != min(concurrency, batch_count):
        raise RuntimeError(
            f"Receiver did not observe the requested batch concurrency: {row}"
        )
    expected_sessions = completed if service == "in_memory" else 0
    if row["delta_sessions"] != expected_sessions or row["delta_tasks"] != completed:
        raise RuntimeError(f"Unexpected store retention counts: {row}")
    return row


async def run_case(args, service, mode, repeat, writer, csv_file):
    context = multiprocessing.get_context("spawn")
    connection, child_connection = context.Pipe()
    process = context.Process(
        target=run_server,
        args=(child_connection, service, str(args.sample_agent), 5.0, mode),
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
            baseline = snapshot(
                connection, service, mode, repeat, 0, 0, args.concurrency, 0
            )
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
                    connection,
                    service,
                    mode,
                    repeat,
                    start + count,
                    count,
                    args.concurrency,
                    elapsed,
                    baseline,
                )
                rows.append(row)
                writer.writerow(row)
                csv_file.flush()
                metric = "rss_bytes" if mode == "rss" else "traced_current_bytes"
                print(
                    f"{mode:11} {service:9} repeat={repeat} requests={start + count:5} "
                    f"memory Δ={row['delta_' + metric] / 1024**2:7.2f} MiB "
                    f"batch_max_active={row['batch_max_active']}",
                    flush=True,
                )
            succeeded = True
            return {
                "service": service,
                "measurement_mode": mode,
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
        "schema_version": SCHEMA_VERSION,
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
        "source": source_info(ROOT.parent, args.sample_agent),
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
        "tracemalloc_overhead_bytes",
        "tracemalloc_enabled",
        "sessions",
        "events",
        "tasks",
        "completed",
        "batch_completed",
        "batch_max_active",
        "max_active_since_baseline",
        "measurement_mode",
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
                # Alternate mode and service order across repetitions.
                modes = (
                    args.measurement_modes
                    if repeat % 2
                    else list(reversed(args.measurement_modes))
                )
                services = (
                    ("in_memory", "null") if repeat % 2 else ("null", "in_memory")
                )
                for mode in modes:
                    for service in services:
                        report["runs"].append(
                            await run_case(
                                args, service, mode, repeat, writer, csv_file
                            )
                        )
                        save()
        report["median_final_deltas"] = summarize(
            report["runs"], args.measurement_modes
        )
        report["status"] = "completed"
    except BaseException as error:
        report.update(status="failed", error=repr(error))
        raise
    finally:
        save()
    print(f"Report: {report_file}")


if __name__ == "__main__":
    asyncio.run(main(arguments()))
