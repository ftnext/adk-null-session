"""Versioned measurement data, provenance, validation, and public export."""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import statistics
import subprocess

from .measurement import MODES, metrics_for

SCHEMA_VERSION = 2
SERVICES = ("in_memory", "null")


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_output(directory, *args):
    try:
        return subprocess.check_output(
            ["git", "-C", str(directory), *args], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def source_info(root, sample_agent):
    sample_root = git_output(sample_agent.parent, "rev-parse", "--show-toplevel")
    sample_path = (
        str(sample_agent.relative_to(sample_root)) if sample_root else sample_agent.name
    )
    files = [
        "benchmark/run.py",
        "benchmark/server.py",
        "benchmark/measurement.py",
        "benchmark/dataset.py",
        "src/adk_null_session/_service.py",
        "src/adk_null_session/__init__.py",
    ]
    return {
        "benchmark_revision": git_output(root, "rev-parse", "HEAD"),
        "working_tree_dirty": bool(git_output(root, "status", "--porcelain")),
        "sha256": {name: sha256(root / name) for name in files},
        "sample": {
            "repository": "https://github.com/google/adk-python",
            "revision": git_output(sample_agent.parent, "rev-parse", "HEAD"),
            "path": sample_path,
            "agent_sha256": sha256(sample_agent),
            "card_sha256": sha256(sample_agent.with_name("agent.json")),
        },
    }


def summarize(runs, modes):
    return {
        mode: {
            service: {
                metric: statistics.median(
                    run["final"][f"delta_{metric}"]
                    for run in runs
                    if run["measurement_mode"] == mode and run["service"] == service
                )
                for metric in metrics_for(mode)
            }
            for service in SERVICES
        }
        for mode in modes
    }


def validate_dataset(report, rows):
    if report.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            "Expected schema version 2; legacy instrumented RSS is not interchangeable"
        )
    if report["status"] != "completed":
        raise ValueError("Only completed benchmarks can be published or plotted")
    config = report["config"]
    modes = config["measurement_modes"]
    if (
        not modes
        or len(set(modes)) != len(modes)
        or any(mode not in MODES for mode in modes)
    ):
        raise ValueError("Invalid measurement modes")
    checkpoints = [
        0,
        *range(config["sample_every"], config["requests"], config["sample_every"]),
        config["requests"],
    ]
    indexed = {}
    for row in rows:
        mode, service = row["measurement_mode"], row["service"]
        repeat, count = int(row["repeat"]), int(row["requests"])
        key = (mode, service, repeat, count)
        if key in indexed:
            raise ValueError(f"Duplicate sample: {key}")
        indexed[key] = row
        if mode not in modes or service not in SERVICES:
            raise ValueError(f"Unexpected sample: {key}")
        enabled = row["tracemalloc_enabled"]
        if enabled != str(mode == "tracemalloc"):
            raise ValueError(f"Incorrect tracing state: {key}")
        trace_fields = (
            "traced_current_bytes",
            "traced_peak_bytes",
            "tracemalloc_overhead_bytes",
        )
        if mode == "rss" and any(row[field] != "" for field in trace_fields):
            raise ValueError("RSS-only runs must not contain traced allocations")
        for metric in metrics_for(mode):
            value, delta = int(row[metric]), int(row[f"delta_{metric}"])
            if count == 0 and delta != 0:
                raise ValueError("Baseline deltas must be zero")
            if value < 0:
                raise ValueError("Absolute measurements must be nonnegative")
        previous = max((n for n in checkpoints if n < count), default=0)
        batch = count - previous
        if int(row["completed"]) != count or int(row["batch_completed"]) != batch:
            raise ValueError(f"Completion count mismatch: {key}")
        if int(row["batch_max_active"]) != min(config["concurrency"], batch):
            raise ValueError(f"Batch concurrency mismatch: {key}")
        if (
            int(row["delta_sessions"]) != (count if service == "in_memory" else 0)
            or int(row["delta_tasks"]) != count
        ):
            raise ValueError(f"Store retention mismatch: {key}")
    expected = {
        (mode, service, repeat, count)
        for mode in modes
        for service in SERVICES
        for repeat in range(1, config["repeats"] + 1)
        for count in checkpoints
    }
    if set(indexed) != expected:
        raise ValueError("Missing or unexpected measurement samples")
    run_keys = set()
    for run in report["runs"]:
        key = (run["measurement_mode"], run["service"], run["repeat"])
        if key in run_keys:
            raise ValueError("Duplicate run in report")
        run_keys.add(key)
        baseline = indexed[(*key, 0)]
        for count in checkpoints:
            row = indexed[(*key, count)]
            for metric in metrics_for(key[0]):
                if int(row[f"delta_{metric}"]) != int(row[metric]) - int(
                    baseline[metric]
                ):
                    raise ValueError(
                        f"Incorrect baseline subtraction: {key}/{count}/{metric}"
                    )
        for endpoint in ("baseline", "final"):
            sample = run[endpoint]
            row = indexed[(*key, sample["requests"])]
            if any(
                row.get(field) != ("" if value is None else str(value))
                for field, value in sample.items()
            ):
                raise ValueError(f"CSV and report disagree: {key}/{endpoint}")
    if run_keys != {key[:3] for key in expected}:
        raise ValueError("Missing or unexpected runs in report")
    if summarize(report["runs"], modes) != report["median_final_deltas"]:
        raise ValueError("Incorrect summary in report")


def load_dataset(directory):
    report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    with (directory / "samples.csv").open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    validate_dataset(report, rows)
    return report, rows


def publish(source, destination):
    report, _ = load_dataset(source)
    public = copy.deepcopy(report)
    public["config"]["sample_agent"] = public["source"]["sample"]["path"]
    public["config"]["output"] = "."
    for run in public["runs"]:
        run.pop("pid", None)
    public["publication"] = {
        "normalization_version": 1,
        "raw_report_sha256": sha256(source / "report.json"),
        "raw_samples_sha256": sha256(source / "samples.csv"),
        "transformations": ["config paths normalized", "run process IDs omitted"],
    }
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "report.json").write_text(
        json.dumps(public, indent=2) + "\n", encoding="utf-8"
    )
    (destination / "samples.csv").write_bytes((source / "samples.csv").read_bytes())
    return public
