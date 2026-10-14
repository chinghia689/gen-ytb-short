"""Lightweight JSONL performance telemetry for newsroom runs."""
from __future__ import annotations

import json
import math
import os
import statistics
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


PROTOTYPE_DIR = Path(__file__).resolve().parent
DEFAULT_LOG = PROTOTYPE_DIR / "data" / "performance.jsonl"
_ACTIVE_RUN: ContextVar["PerformanceRun | None"] = ContextVar("performance_run", default=None)


class PerformanceRun:
    def __init__(self, *, limit: int, log_path: Path | None = None) -> None:
        self.run_id = str(uuid.uuid4())
        self.limit = limit
        self.log_path = log_path or Path(os.getenv("PERFORMANCE_LOG", DEFAULT_LOG))
        self.started_at = datetime.now(timezone.utc)
        self.started_clock = time.perf_counter()
        self.stages: list[dict[str, Any]] = []
        self.counters: dict[str, int] = {}
        self.processed: list[dict[str, Any]] = []
        self._token = None

    def __enter__(self) -> "PerformanceRun":
        self._token = _ACTIVE_RUN.set(self)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        total = time.perf_counter() - self.started_clock
        _ACTIVE_RUN.reset(self._token)
        record = {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": round(total, 6),
            "limit": self.limit,
            "status": "error" if exc else "complete",
            "counters": self.counters,
            "stages": self.stages,
            "videos": self.processed,
        }
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(record, ensure_ascii=False) + "\n")

    def count(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount

    def stage(self, name: str, **metadata: Any):
        return _Stage(self, name, metadata)


class _Stage:
    def __init__(self, run: PerformanceRun | None, name: str, metadata: dict[str, Any]) -> None:
        self.run = run
        self.name = name
        self.metadata = metadata
        self.started_clock = 0.0
        self.started_at = ""

    def __enter__(self) -> "_Stage":
        self.started_clock = time.perf_counter()
        self.started_at = datetime.now(timezone.utc).isoformat()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if not self.run:
            return
        ended = datetime.now(timezone.utc)
        elapsed = time.perf_counter() - self.started_clock
        self.run.stages.append({
            "stage": self.name,
            "started_at": self.started_at,
            "finished_at": ended.isoformat(),
            "duration_seconds": round(elapsed, 6),
            "status": "error" if exc else "complete",
            **self.metadata,
            **({"error": str(exc)[:500]} if exc else {}),
        })
        if exc:
            self.run.count("stage_errors")
            if isinstance(exc, (TimeoutError,)) or "timeout" in type(exc).__name__.casefold():
                self.run.count("timeouts")


@contextmanager
def stage(name: str, **metadata: Any) -> Iterator[None]:
    run = _ACTIVE_RUN.get()
    with _Stage(run, name, metadata):
        yield


def count(name: str, amount: int = 1) -> None:
    run = _ACTIVE_RUN.get()
    if run:
        run.count(name, amount)


def active_run() -> PerformanceRun | None:
    return _ACTIVE_RUN.get()


def summarize(path: Path = DEFAULT_LOG, *, last: int = 100) -> dict[str, Any]:
    records = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines()[-last:]:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    grouped: dict[str, list[float]] = {}
    for record in records:
        for item in record.get("stages", []):
            name = str(item.get("stage", "unknown"))
            if item.get("source"):
                name = f"{name}[{item['source']}]"
            grouped.setdefault(name, []).append(float(item.get("duration_seconds", 0)))
    stage_stats = {}
    for name, values in grouped.items():
        ordered = sorted(values)
        stage_stats[name] = {
            "count": len(values),
            "mean_seconds": round(statistics.fmean(values), 3),
            "p50_seconds": round(statistics.median(values), 3),
            "p95_seconds": round(ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)], 3),
        }
    durations = [float(record.get("duration_seconds", 0)) for record in records]
    story_durations = [
        float(item.get("duration_seconds", 0))
        for record in records for item in record.get("videos", [])
    ]
    completed_durations = [
        float(item.get("duration_seconds", 0))
        for record in records for item in record.get("videos", [])
        if item.get("video_completed")
    ]
    counter_totals: Counter[str] = Counter()
    review_outcomes: Counter[str] = Counter()
    script_methods: Counter[str] = Counter()
    for record in records:
        counter_totals.update(record.get("counters", {}))
        for item in record.get("videos", []):
            if item.get("ai_review_status"):
                review_outcomes[str(item["ai_review_status"])] += 1
            if item.get("script_generation_method"):
                script_methods[str(item["script_generation_method"])] += 1

    def distribution(values: list[float]) -> dict[str, float]:
        ordered = sorted(values)
        return {
            "mean": round(statistics.fmean(values), 3),
            "p50": round(statistics.median(values), 3),
            "p95": round(ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)], 3),
        } if values else {}

    return {
        "log": str(path),
        "runs": len(records),
        "total_run_seconds": distribution(durations),
        "per_story_seconds": distribution(story_durations),
        "completed_video_seconds": distribution(completed_durations),
        "stages": stage_stats,
        "counters": dict(counter_totals),
        "quality": {
            "ai_review_outcomes": dict(review_outcomes),
            "script_generation_methods": dict(script_methods),
        },
    }
