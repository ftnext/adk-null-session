"""Quiescence, interval counters, and memory sampling without ADK dependencies."""

from __future__ import annotations

import asyncio
import gc
import tracemalloc

MODES = ("rss", "tracemalloc")
STORE_METRICS = ("sessions", "events", "tasks")


def metrics_for(mode):
    if mode == "rss":
        return ("rss_bytes", *STORE_METRICS)
    if mode == "tracemalloc":
        return (
            "traced_current_bytes",
            "traced_peak_bytes",
            "tracemalloc_overhead_bytes",
            *STORE_METRICS,
        )
    raise ValueError(f"Unknown measurement mode: {mode}")


class RequestTracker:
    def __init__(self):
        self.idle = asyncio.Event()
        self.idle.set()
        self.active = 0
        self.reset()

    def reset(self):
        if self.active:
            raise RuntimeError("Cannot reset counters during active requests")
        self.completed = self.batch_completed = 0
        self.batch_max_active = self.max_active_since_baseline = 0

    def begin(self):
        self.idle.clear()
        self.active += 1
        self.batch_max_active = max(self.batch_max_active, self.active)
        self.max_active_since_baseline = max(
            self.max_active_since_baseline, self.active
        )

    def finish(self, succeeded):
        if succeeded:
            self.completed += 1
            self.batch_completed += 1
        self.active -= 1
        if self.active == 0:
            self.idle.set()

    async def wait_idle(self, timeout=60):
        await asyncio.wait_for(self.idle.wait(), timeout)

    def take_interval(self):
        result = {
            "completed": self.completed,
            "batch_completed": self.batch_completed,
            "batch_max_active": self.batch_max_active,
            "max_active_since_baseline": self.max_active_since_baseline,
        }
        self.batch_completed = self.batch_max_active = 0
        return result


def read_memory(mode, process, baseline=False):
    tracing = tracemalloc.is_tracing()
    if tracing != (mode == "tracemalloc"):
        raise RuntimeError(f"Unexpected tracemalloc state in {mode} mode: {tracing}")
    current = peak = overhead = None
    if tracing:
        if baseline:
            tracemalloc.reset_peak()
        current, peak = tracemalloc.get_traced_memory()
        overhead = tracemalloc.get_tracemalloc_memory()
    return {
        "rss_bytes": process.memory_info().rss,
        "traced_current_bytes": current,
        "traced_peak_bytes": peak,
        "tracemalloc_overhead_bytes": overhead,
        "tracemalloc_enabled": tracing,
    }


async def take_snapshot(tracker, mode, process, count_stores, baseline=False):
    await tracker.wait_idle()
    if baseline:
        tracker.reset()
    gc.collect()
    # Capture memory before traversing stores or constructing counter summaries.
    memory = read_memory(mode, process, baseline)
    counts = count_stores()
    return {**memory, **counts, **tracker.take_interval()}
