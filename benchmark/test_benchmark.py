"""Regression checks for benchmark instrumentation and published evidence."""

import asyncio
import copy
import csv
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmark import measurement
from benchmark.dataset import load_dataset, publish, source_info, summarize
from benchmark.run import SAMPLE, arguments, snapshot


@pytest.fixture
def dataset(tmp_path):
    source = tmp_path / "raw"
    source.mkdir()
    rows, runs = [], []
    for mode in ("rss", "tracemalloc"):
        for service in ("in_memory", "null"):
            for repeat in (1, 2):
                samples = []
                for count in (0, 3, 4):
                    rss_unit = 1_000_000 if service == "in_memory" else 300_000
                    trace_unit = 700_000 if service == "in_memory" else 200_000
                    # Diagnostic RSS in traced runs must never feed the RSS chart.
                    rss_unit *= 5 if mode == "tracemalloc" else 1
                    row = dict(
                        service=service,
                        measurement_mode=mode,
                        repeat=repeat,
                        requests=count,
                        elapsed_seconds=float(count),
                        rss_bytes=100_000 + count * rss_unit + (repeat if count else 0),
                        traced_current_bytes=(
                            50_000 + count * trace_unit + repeat
                            if mode == "tracemalloc"
                            else None
                        ),
                        traced_peak_bytes=(
                            60_000 + count * trace_unit + repeat
                            if mode == "tracemalloc"
                            else None
                        ),
                        tracemalloc_overhead_bytes=(
                            10_000 + count * 100 if mode == "tracemalloc" else None
                        ),
                        tracemalloc_enabled=(mode == "tracemalloc"),
                        sessions=(3 + count if service == "in_memory" else 0),
                        events=(4 * (3 + count) if service == "in_memory" else 0),
                        tasks=3 + count,
                        completed=count,
                        batch_completed=(3 if count == 3 else min(count, 1)),
                        batch_max_active=(3 if count == 3 else min(count, 1)),
                        max_active_since_baseline=(3 if count else 0),
                    )
                    baseline = samples[0] if samples else row
                    for metric in measurement.metrics_for(mode):
                        row[f"delta_{metric}"] = row[metric] - baseline[metric]
                    samples.append(row)
                    rows.append(row)
                runs.append(
                    dict(
                        service=service,
                        measurement_mode=mode,
                        repeat=repeat,
                        pid=123,
                        baseline=samples[0],
                        final=samples[-1],
                    )
                )
    report = dict(
        schema_version=2,
        status="completed",
        config=dict(
            measurement_modes=["rss", "tracemalloc"],
            requests=4,
            sample_every=3,
            repeats=2,
            concurrency=3,
            warmup=3,
            sample_agent="/private/local/sample/agent.py",
            output=str(source),
        ),
        source=dict(sample=dict(path="contributing/sample/agent.py")),
        runs=runs,
        median_final_deltas=summarize(runs, ["rss", "tracemalloc"]),
    )
    (source / "report.json").write_text(json.dumps(report))
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with (source / "samples.csv").open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return source


@pytest.mark.parametrize("mode", ["rss", "tracemalloc"])
def test_memory_mode_and_sampling_before_store_traversal(monkeypatch, mode):
    calls = []
    monkeypatch.setattr(measurement.gc, "collect", lambda: calls.append("gc"))
    monkeypatch.setattr(
        measurement.tracemalloc, "is_tracing", lambda: mode == "tracemalloc"
    )
    monkeypatch.setattr(
        measurement.tracemalloc, "reset_peak", lambda: calls.append("reset")
    )

    def traced():
        calls.append("traced")
        return 100, 120

    monkeypatch.setattr(measurement.tracemalloc, "get_traced_memory", traced)
    monkeypatch.setattr(measurement.tracemalloc, "get_tracemalloc_memory", lambda: 30)

    def rss():
        calls.append("rss")
        return SimpleNamespace(rss=999)

    def stores():
        calls.append("stores")
        return dict(sessions=0, events=0, tasks=0)

    async def exercise():
        return await measurement.take_snapshot(
            measurement.RequestTracker(),
            mode,
            SimpleNamespace(memory_info=rss),
            stores,
            True,
        )

    row = asyncio.run(exercise())
    assert calls == (
        ["gc", "reset", "traced", "rss", "stores"]
        if mode == "tracemalloc"
        else ["gc", "rss", "stores"]
    )
    assert row["traced_current_bytes"] == (100 if mode == "tracemalloc" else None)
    assert row["tracemalloc_overhead_bytes"] == (30 if mode == "tracemalloc" else None)


def test_wrong_tracing_state_is_rejected(monkeypatch):
    monkeypatch.setattr(measurement.tracemalloc, "is_tracing", lambda: True)
    with pytest.raises(RuntimeError, match="Unexpected tracemalloc state"):
        measurement.read_memory("rss", None)


@pytest.mark.asyncio
async def test_quiescence_waits_for_completion_instead_of_a_fixed_delay():
    tracker = measurement.RequestTracker()
    tracker.begin()
    waiter = asyncio.create_task(tracker.wait_idle(timeout=1))
    await asyncio.sleep(0.03)
    assert not waiter.done()
    tracker.finish(True)
    await waiter
    tracker.begin()
    with pytest.raises(TimeoutError):
        await tracker.wait_idle(timeout=0.001)
    tracker.finish(False)
    assert tracker.completed == 1


def test_batch_peak_resets_without_resetting_run_peak():
    tracker = measurement.RequestTracker()
    for _ in range(3):
        tracker.begin()
    for _ in range(3):
        tracker.finish(True)
    assert tracker.take_interval()["batch_max_active"] == 3
    for _ in range(3):
        tracker.begin()
        tracker.finish(True)
    interval = tracker.take_interval()
    assert interval["batch_max_active"] == 1
    assert interval["max_active_since_baseline"] == 3
    assert interval["batch_completed"] == 3


def test_snapshot_rejects_serial_batch_despite_earlier_concurrent_batch():
    baseline = dict(rss_bytes=100, sessions=0, events=0, tasks=0)
    row = dict(
        rss_bytes=110,
        sessions=3,
        events=12,
        tasks=3,
        completed=3,
        batch_completed=3,
        batch_max_active=1,
        max_active_since_baseline=3,
    )
    connection = SimpleNamespace(
        send=lambda _: None, poll=lambda _: True, recv=lambda: row
    )
    with pytest.raises(RuntimeError, match="batch concurrency"):
        snapshot(connection, "in_memory", "rss", 1, 3, 3, 3, 1, baseline)


def test_public_export_preserves_measurements_and_is_reproducible(dataset, tmp_path):
    raw_bytes = (dataset / "report.json").read_bytes()
    raw, _ = load_dataset(dataset)
    public = publish(dataset, tmp_path / "public")
    publish(dataset, tmp_path / "public-again")
    assert public["median_final_deltas"] == raw["median_final_deltas"]
    assert public["source"] == raw["source"]
    assert public["config"]["sample_agent"] == "contributing/sample/agent.py"
    assert "pid" not in public["runs"][0]
    assert (
        public["publication"]["raw_report_sha256"]
        == hashlib.sha256(raw_bytes).hexdigest()
    )
    assert (dataset / "report.json").read_bytes() == raw_bytes
    for name in ("report.json", "samples.csv"):
        assert (tmp_path / "public" / name).read_bytes() == (
            tmp_path / "public-again" / name
        ).read_bytes()
    load_dataset(tmp_path / "public")


@pytest.mark.parametrize(
    "corruption",
    ["incomplete", "duplicate", "missing", "mode", "batch", "summary", "value"],
)
def test_invalid_evidence_is_rejected_before_publication(dataset, tmp_path, corruption):
    report = json.loads((dataset / "report.json").read_text())
    with (dataset / "samples.csv").open(newline="") as file:
        reader = csv.DictReader(file)
        rows, fields = list(reader), reader.fieldnames
    if corruption == "incomplete":
        report["status"] = "failed"
    elif corruption == "duplicate":
        rows.append(copy.deepcopy(rows[-1]))
    elif corruption == "missing":
        rows.pop()
    elif corruption == "mode":
        rows[1]["tracemalloc_enabled"] = "True"
    elif corruption == "batch":
        rows[1]["batch_max_active"] = "1"
    elif corruption == "summary":
        report["median_final_deltas"]["rss"]["null"]["rss_bytes"] += 1
    else:
        rows[1]["delta_rss_bytes"] = str(int(rows[1]["delta_rss_bytes"]) + 1)
    (dataset / "report.json").write_text(json.dumps(report))
    with (dataset / "samples.csv").open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    destination = tmp_path / "rejected"
    with pytest.raises(ValueError):
        publish(dataset, destination)
    assert not destination.exists()


def test_source_provenance_is_recorded_automatically(tmp_path):
    sample = tmp_path / "agent.py"
    sample.write_text("root_agent = None\n")
    sample.with_name("agent.json").write_text("{}")
    root = Path(__file__).resolve().parents[1]
    source = source_info(root, sample)
    assert (
        source["sample"]["agent_sha256"]
        == hashlib.sha256(sample.read_bytes()).hexdigest()
    )
    for name, digest in source["sha256"].items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
    assert SAMPLE.is_relative_to(root / "benchmark/.samples")
    assert arguments(
        ["--sample-agent", str(sample), "--concurrency", "1"]
    ).measurement_modes == ["rss", "tracemalloc"]


def test_charts_use_independent_rss_runs_and_even_repeat_medians(
    dataset, tmp_path, monkeypatch
):
    monkeypatch.setenv("MPLCONFIGDIR", str(tmp_path / "matplotlib"))
    pytest.importorskip("matplotlib")
    from benchmark import plot

    report, series = plot.load_data(dataset)
    assert series["in_memory"][4]["delta_rss_bytes"][0] == 4_000_001.5 / 1024**2
    assert (
        series["in_memory"][4]["delta_traced_current_bytes"][0] == 2_800_000 / 1024**2
    )
    output = tmp_path / "charts"
    output.mkdir()
    plot.render(report, series, output, "light", None)
    first = {path.name: path.read_bytes() for path in output.glob("*.svg")}
    plot.render(report, series, output, "light", None)
    assert first == {path.name: path.read_bytes() for path in output.glob("*.svg")}
    assert b"RSS" in first["memory-comparison.svg"]
    assert b"tracing off" in first["memory-comparison.svg"]
