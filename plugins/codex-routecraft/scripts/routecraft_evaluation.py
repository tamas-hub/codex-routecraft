#!/usr/bin/env python3
"""Local-only effectiveness evaluation for RouteCraft persistent decision memory.

The evaluator deliberately stores no raw prompts, queries, source code, logs,
credentials, or absolute user paths. Events live under ~/.codex/routecraft by
default and are not synchronized through the Decision Store.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

SCRIPT_PATH = Path(__file__).resolve()
SCRIPTS_DIR = SCRIPT_PATH.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from routecraft_memory_lib.base import load_config, resolve_device_id  # noqa: E402
from routecraft_memory_lib.records import load_records  # noqa: E402
from routecraft_memory_lib.search import recall_records  # noqa: E402

EVAL_SCHEMA_VERSION = 1
DEFAULT_MODE = "full"
VALID_MODES = ("off", "recall", "full")
VALID_TASK_CLASSES = ("general", "debugging", "implementation", "ci", "refactor", "docs", "release", "integration", "test")
VALID_RISKS = ("low", "medium", "high", "critical")
VALID_OUTCOMES = ("success", "partial", "failed", "cancelled")
RECORD_ID_RE = re.compile(r"^(?:CASE|CAND|RULE)-[A-Z0-9-]+$")
SAFE_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ABSOLUTE_USER_PATH_RE = re.compile(r"(?:[A-Za-z]:\\Users\\|/Users/[^/]+/|/home/[^/]+/)", re.I)
FORBIDDEN_EVENT_KEYS = {
    "query", "prompt", "conversation", "transcript", "log", "cwd", "home",
    "absolute_path", "token", "password", "credential", "secret", "private_key",
}
LOCK_STALE_SECONDS = 5 * 60
MAX_EVENT_BYTES = 64_000


class EvaluationError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def default_eval_dir() -> Path:
    override = os.environ.get("ROUTECRAFT_EVALUATION_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".codex" / "routecraft" / "evaluation").resolve()


def config_path(base: Path) -> Path:
    return base / "config.json"


def events_path(base: Path) -> Path:
    return base / "events.jsonl"


def benchmark_last_path(base: Path) -> Path:
    return base / "benchmark-last.json"


def ensure_dir(base: Path) -> None:
    base.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(base, 0o700)


class EvalLock:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.path = base / ".lock"
        self.acquired = False

    def __enter__(self) -> "EvalLock":
        ensure_dir(self.base)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w", encoding="ascii") as handle:
                    handle.write(f"{os.getpid()} {time.time()}\n")
                self.acquired = True
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age > LOCK_STALE_SECONDS:
                    with contextlib.suppress(FileNotFoundError):
                        self.path.unlink()
                    continue
                raise EvaluationError(f"evaluation log is locked: {self.path}")
        raise EvaluationError("could not acquire evaluation log lock")

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.acquired:
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()


def atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    ensure_dir(path.parent)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(OSError):
            os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()


def default_config() -> dict[str, Any]:
    return {
        "schema_version": EVAL_SCHEMA_VERSION,
        "enabled": False,
        "mode": DEFAULT_MODE,
        "experiment": {
            "enabled": False,
            "strategy": "round-robin",
            "sequence": ["off", "recall", "full"],
            "counter": 0,
        },
    }


def load_eval_config(base: Path) -> dict[str, Any]:
    path = config_path(base)
    if not path.is_file():
        return default_config()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"invalid evaluation config: {exc}") from exc
    if not isinstance(data, dict):
        raise EvaluationError("evaluation config must be a JSON object")
    merged = default_config()
    merged.update(data)
    exp = default_config()["experiment"]
    exp.update(data.get("experiment") or {})
    merged["experiment"] = exp
    if merged.get("mode") not in VALID_MODES:
        raise EvaluationError(f"invalid evaluation mode: {merged.get('mode')!r}")
    sequence = exp.get("sequence") or []
    if not sequence or any(item not in VALID_MODES for item in sequence):
        raise EvaluationError("experiment sequence must contain only off/recall/full")
    return merged


def save_eval_config(base: Path, config: Mapping[str, Any]) -> None:
    atomic_json(config_path(base), config)


def normalize_repository(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    raw = re.sub(r"^git@github\.com:", "", raw, flags=re.I)
    raw = re.sub(r"^ssh://git@github\.com/", "", raw, flags=re.I)
    raw = re.sub(r"^https?://github\.com/", "", raw, flags=re.I)
    raw = raw.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if raw.endswith(".git"):
        raw = raw[:-4]
    parts = [part for part in raw.split("/") if part]
    if len(parts) >= 2:
        candidate = f"{parts[-2]}/{parts[-1]}"
        if SAFE_REPOSITORY_RE.fullmatch(candidate):
            return candidate
    return ""


def repository_from_path(path: Path) -> str:
    try:
        process = subprocess.run(
            ["git", "-C", str(path), "remote", "get-url", "origin"],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError:
        return ""
    return normalize_repository(process.stdout) if process.returncode == 0 else ""


def evaluation_device_id() -> str:
    try:
        return resolve_device_id(load_config())
    except Exception:
        return "device"


def validate_record_id(value: str) -> str:
    candidate = value.strip().upper()
    if not RECORD_ID_RE.fullmatch(candidate):
        raise EvaluationError(f"invalid record id: {value!r}")
    return candidate


def validate_event(payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_EVENT_BYTES:
        raise EvaluationError("evaluation event too large")
    if ABSOLUTE_USER_PATH_RE.search(encoded):
        raise EvaluationError("absolute user path rejected from evaluation event")

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                normalized = str(key).lower()
                if normalized in FORBIDDEN_EVENT_KEYS:
                    raise EvaluationError(f"forbidden evaluation field: {key}")
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(payload)


def append_event(base: Path, payload: Mapping[str, Any]) -> None:
    validate_event(payload)
    ensure_dir(base)
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    with EvalLock(base):
        with events_path(base).open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(OSError):
            os.chmod(events_path(base), 0o600)


def load_events(base: Path) -> tuple[list[dict[str, Any]], int]:
    path = events_path(base)
    if not path.is_file():
        return [], 0
    events: list[dict[str, Any]] = []
    malformed = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError
            events.append(item)
        except (json.JSONDecodeError, ValueError):
            malformed += 1
    return events, malformed


def next_mode(base: Path, config: dict[str, Any], override: str | None = None) -> str:
    if override:
        if override not in VALID_MODES:
            raise EvaluationError(f"invalid mode: {override}")
        return override
    experiment = config.get("experiment") or {}
    if not experiment.get("enabled"):
        return str(config.get("mode") or DEFAULT_MODE)
    with EvalLock(base):
        current = load_eval_config(base)
        current_experiment = current.get("experiment") or {}
        sequence = list(current_experiment.get("sequence") or [])
        counter = int(current_experiment.get("counter", 0))
        mode = sequence[counter % len(sequence)]
        current_experiment["counter"] = counter + 1
        current["experiment"] = current_experiment
        save_eval_config(base, current)
    return mode


def make_task_id(device_id: str) -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_device = re.sub(r"[^A-Za-z0-9]+", "", device_id).upper()[:10] or "DEVICE"
    entropy = f"{os.getpid()}|{time.time_ns()}".encode("ascii", errors="ignore")
    import hashlib
    suffix = hashlib.sha256(entropy).hexdigest()[:6].upper()
    return f"EVAL-{stamp}-{safe_device}-{suffix}"


def start_task(base: Path, *, repository: str, task_class: str, risk: str, mode_override: str | None = None) -> dict[str, Any]:
    config = load_eval_config(base)
    if not bool(config.get("enabled")):
        return {"schema_version": 1, "tracking": False, "mode": str(config.get("mode") or DEFAULT_MODE)}
    mode = next_mode(base, config, mode_override)
    device_id = evaluation_device_id()
    task_id = make_task_id(device_id)
    event = {
        "schema_version": EVAL_SCHEMA_VERSION,
        "event": "task_start",
        "ts": utc_now(),
        "task_id": task_id,
        "mode": mode,
        "repository": normalize_repository(repository),
        "task_class": task_class,
        "risk": risk,
        "device_id": device_id,
    }
    append_event(base, event)
    return {"schema_version": 1, "tracking": True, "task_id": task_id, "mode": mode, "repository": event["repository"], "task_class": task_class}


def record_lookup(store: Path) -> dict[str, Any]:
    return {record.record_id: record for record in load_records(store)}


def record_recall(base: Path, *, task_id: str, store: Path, ranked_ids: Sequence[tuple[str, int]]) -> dict[str, Any]:
    records = record_lookup(store)
    device_id = evaluation_device_id()
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record_id, rank in ranked_ids:
        rid = validate_record_id(record_id)
        if rid in seen:
            continue
        seen.add(rid)
        record = records.get(rid)
        if record is None:
            raise EvaluationError(f"recalled record not found in store: {rid}")
        selected.append({
            "id": rid,
            "rank": int(rank),
            "kind": record.kind,
            "repository": normalize_repository(str(record.metadata.get("repository", ""))),
            "source_device_id": str(record.metadata.get("device_id", ""))[:32],
        })
    event = {"schema_version": EVAL_SCHEMA_VERSION, "event": "recall", "ts": utc_now(), "task_id": task_id, "device_id": device_id, "match_count": len(selected), "records": selected}
    append_event(base, event)
    return {"tracking": True, "task_id": task_id, "record_count": len(selected)}


def find_task_start(events: Sequence[Mapping[str, Any]], task_id: str) -> Mapping[str, Any] | None:
    for event in reversed(events):
        if event.get("event") == "task_start" and event.get("task_id") == task_id:
            return event
    return None


def finish_task(base: Path, *, task_id: str, outcome: str, elapsed_seconds: float | None, tool_calls: int | None, failed_hypotheses: int | None, useful_records: Sequence[str], misleading_records: Sequence[str], stale_records: Sequence[str], learned_records: Sequence[str], source_chars: int | None, record_chars: int | None) -> dict[str, Any]:
    events, _ = load_events(base)
    start = find_task_start(events, task_id)
    if start is None:
        raise EvaluationError(f"task start not found: {task_id}")
    if elapsed_seconds is None:
        elapsed_seconds = max(0.0, (parse_time(utc_now()) - parse_time(str(start["ts"]))).total_seconds())
    categories = {
        "useful_records": [validate_record_id(item) for item in useful_records],
        "misleading_records": [validate_record_id(item) for item in misleading_records],
        "stale_records": [validate_record_id(item) for item in stale_records],
        "learned_records": [validate_record_id(item) for item in learned_records],
    }
    verdict_ids = categories["useful_records"] + categories["misleading_records"] + categories["stale_records"]
    if len(set(verdict_ids)) != len(verdict_ids):
        raise EvaluationError("a recalled record cannot have more than one final verdict")
    event: dict[str, Any] = {"schema_version": EVAL_SCHEMA_VERSION, "event": "task_finish", "ts": utc_now(), "task_id": task_id, "outcome": outcome, "elapsed_seconds": round(float(elapsed_seconds), 3), **categories}
    if tool_calls is not None:
        event["tool_calls"] = int(tool_calls)
    if failed_hypotheses is not None:
        event["failed_hypotheses"] = int(failed_hypotheses)
    if source_chars is not None:
        event["source_chars"] = int(source_chars)
    if record_chars is not None:
        event["record_chars"] = int(record_chars)
    append_event(base, event)
    return {"tracking": True, "task_id": task_id, "outcome": outcome, "elapsed_seconds": event["elapsed_seconds"]}


def median(values: Sequence[float]) -> float | None:
    return round(float(statistics.median(values)), 3) if values else None


def mean(values: Sequence[float]) -> float | None:
    return round(float(statistics.fmean(values)), 3) if values else None


def pct_reduction(baseline: float | None, current: float | None) -> float | None:
    if baseline is None or current is None or baseline <= 0:
        return None
    return round((baseline - current) / baseline * 100.0, 2)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def privacy_scan(events: Sequence[Mapping[str, Any]]) -> int:
    violations = 0
    for event in events:
        try:
            validate_event(event)
        except EvaluationError:
            violations += 1
    return violations


def summarize(base: Path) -> dict[str, Any]:
    events, malformed = load_events(base)
    starts: dict[str, Mapping[str, Any]] = {}
    recalls: dict[str, list[Mapping[str, Any]]] = {}
    finishes: dict[str, Mapping[str, Any]] = {}
    for event in events:
        task_id = str(event.get("task_id", ""))
        if not task_id:
            continue
        if event.get("event") == "task_start":
            starts[task_id] = event
        elif event.get("event") == "recall":
            recalls.setdefault(task_id, []).append(event)
        elif event.get("event") == "task_finish":
            finishes[task_id] = event

    completed: list[dict[str, Any]] = []
    recalled_records = useful_records = misleading_records = stale_records = useful_recall_tasks = 0
    reciprocal_ranks: list[float] = []
    cross_project_useful = cross_device_useful = known_project_useful = known_device_useful = 0
    task_repositories: set[str] = set()
    task_devices: set[str] = set()
    source_devices: set[str] = set()

    for task_id, finish in finishes.items():
        start = starts.get(task_id)
        if not start:
            continue
        task = {
            "task_id": task_id,
            "mode": str(start.get("mode", "")),
            "repository": str(start.get("repository", "")),
            "task_class": str(start.get("task_class", "general")),
            "device_id": str(start.get("device_id", "")),
            "outcome": str(finish.get("outcome", "")),
            "elapsed_seconds": float(finish.get("elapsed_seconds", 0)),
            "tool_calls": finish.get("tool_calls"),
            "failed_hypotheses": finish.get("failed_hypotheses"),
        }
        completed.append(task)
        if task["repository"]:
            task_repositories.add(task["repository"])
        if task["device_id"]:
            task_devices.add(task["device_id"])

        useful = set(finish.get("useful_records") or [])
        misleading = set(finish.get("misleading_records") or [])
        stale = set(finish.get("stale_records") or [])
        useful_records += len(useful)
        misleading_records += len(misleading)
        stale_records += len(stale)
        ranked: dict[str, Mapping[str, Any]] = {}
        for recall in recalls.get(task_id, []):
            for record in recall.get("records") or []:
                rid = str(record.get("id", ""))
                rank = int(record.get("rank", 999))
                if rid not in ranked or rank < int(ranked[rid].get("rank", 999)):
                    ranked[rid] = record
        recalled_records += len(ranked)
        useful_ranks = [int(ranked[rid].get("rank", 999)) for rid in useful if rid in ranked]
        if useful_ranks:
            useful_recall_tasks += 1
            reciprocal_ranks.append(1.0 / min(useful_ranks))
        elif ranked:
            reciprocal_ranks.append(0.0)

        for rid in useful:
            record = ranked.get(rid)
            if not record:
                continue
            source_repo = str(record.get("repository", ""))
            source_device = str(record.get("source_device_id", ""))
            if source_repo and task["repository"]:
                known_project_useful += 1
                if source_repo != task["repository"]:
                    cross_project_useful += 1
            if source_device:
                source_devices.add(source_device)
            if source_device and task["device_id"]:
                known_device_useful += 1
                if source_device != task["device_id"]:
                    cross_device_useful += 1

    by_mode: dict[str, dict[str, Any]] = {}
    for mode in VALID_MODES:
        rows = [task for task in completed if task["mode"] == mode]
        elapsed = [task["elapsed_seconds"] for task in rows if task["elapsed_seconds"] >= 0]
        tools = [float(task["tool_calls"]) for task in rows if isinstance(task.get("tool_calls"), int)]
        hypotheses = [float(task["failed_hypotheses"]) for task in rows if isinstance(task.get("failed_hypotheses"), int)]
        by_mode[mode] = {
            "tasks": len(rows),
            "successes": sum(1 for task in rows if task["outcome"] == "success"),
            "median_elapsed_seconds": median(elapsed),
            "mean_tool_calls": mean(tools),
            "mean_failed_hypotheses": mean(hypotheses),
        }

    baseline = by_mode["off"]
    comparison: dict[str, Any] = {}
    for mode in ("recall", "full"):
        current = by_mode[mode]
        comparison[mode] = {
            "time_reduction_percent": pct_reduction(baseline["median_elapsed_seconds"], current["median_elapsed_seconds"]) if baseline["tasks"] >= 3 and current["tasks"] >= 3 else None,
            "failed_hypothesis_reduction_percent": pct_reduction(baseline["mean_failed_hypotheses"], current["mean_failed_hypotheses"]) if baseline["tasks"] >= 3 and current["tasks"] >= 3 else None,
            "tool_call_reduction_percent": pct_reduction(baseline["mean_tool_calls"], current["mean_tool_calls"]) if baseline["tasks"] >= 3 and current["tasks"] >= 3 else None,
            "comparison_ready": baseline["tasks"] >= 3 and current["tasks"] >= 3,
        }

    recall_task_count = sum(1 for task_id in finishes if recalls.get(task_id))
    useful_task_rate = useful_recall_tasks / recall_task_count if recall_task_count else None
    observed_precision = useful_records / recalled_records if recalled_records else None
    mrr_useful = statistics.fmean(reciprocal_ranks) if reciprocal_ranks else None

    source_chars = sum(int(finish.get("source_chars", 0)) for finish in finishes.values() if int(finish.get("source_chars", 0) or 0) > 0 and int(finish.get("record_chars", 0) or 0) >= 0)
    record_chars = sum(int(finish.get("record_chars", 0)) for finish in finishes.values() if int(finish.get("source_chars", 0) or 0) > 0 and int(finish.get("record_chars", 0) or 0) >= 0)
    compression_ratio = (1.0 - record_chars / source_chars) if source_chars else None

    estimated_saved_seconds = 0.0
    baseline_by_class: dict[str, float] = {}
    for task_class in VALID_TASK_CLASSES:
        values = [task["elapsed_seconds"] for task in completed if task["mode"] == "off" and task["task_class"] == task_class and task["outcome"] == "success"]
        if len(values) >= 2:
            baseline_by_class[task_class] = float(statistics.median(values))
    for task in completed:
        if task["mode"] in {"recall", "full"} and task["outcome"] == "success":
            baseline_value = baseline_by_class.get(task["task_class"])
            if baseline_value is not None:
                estimated_saved_seconds += max(0.0, baseline_value - task["elapsed_seconds"])

    privacy_violations = privacy_scan(events) + malformed
    benchmark: dict[str, Any] = {}
    path = benchmark_last_path(base)
    if path.is_file():
        with contextlib.suppress(OSError, json.JSONDecodeError):
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                benchmark = data

    metrics = {
        "completed_tasks": len(completed),
        "recall_tasks": recall_task_count,
        "recalled_records": recalled_records,
        "useful_records": useful_records,
        "misleading_records": misleading_records,
        "stale_records": stale_records,
        "useful_task_rate": round(useful_task_rate, 4) if useful_task_rate is not None else None,
        "observed_precision": round(observed_precision, 4) if observed_precision is not None else None,
        "mrr_useful": round(mrr_useful, 4) if mrr_useful is not None else None,
        "cross_project_useful": cross_project_useful,
        "cross_device_useful": cross_device_useful,
        "cross_project_rate": round(cross_project_useful / known_project_useful, 4) if known_project_useful else None,
        "cross_device_rate": round(cross_device_useful / known_device_useful, 4) if known_device_useful else None,
        "estimated_saved_seconds": round(estimated_saved_seconds, 1),
        "decision_compression_ratio": round(compression_ratio, 4) if compression_ratio is not None else None,
        "privacy_violations": privacy_violations,
        "malformed_events": malformed,
    }

    components: list[dict[str, Any]] = []
    def component(name: str, maximum: int, score: float | None, sample: int, note: str) -> None:
        components.append({"name": name, "max": maximum, "score": round(score, 2) if score is not None else None, "sample": sample, "note": note})

    if recall_task_count >= 3 and useful_task_rate is not None and mrr_useful is not None:
        component("retrieval_quality", 20, 12 * useful_task_rate + 8 * mrr_useful, recall_task_count, "live verified-use feedback")
    else:
        component("retrieval_quality", 20, None, recall_task_count, "needs >=3 recall tasks")

    full_cmp = comparison["full"] if comparison["full"]["comparison_ready"] else comparison["recall"]
    target_mode = "full" if comparison["full"]["comparison_ready"] else "recall"
    if full_cmp["comparison_ready"] and full_cmp["time_reduction_percent"] is not None:
        component("task_time_reduction", 20, 20 * clamp(float(full_cmp["time_reduction_percent"]) / 40.0), by_mode[target_mode]["tasks"], f"vs off; {target_mode}")
    else:
        component("task_time_reduction", 20, None, min(baseline["tasks"], by_mode[target_mode]["tasks"]), "needs >=3 off and >=3 memory tasks")

    if full_cmp["comparison_ready"] and full_cmp["failed_hypothesis_reduction_percent"] is not None:
        component("failed_hypothesis_reduction", 15, 15 * clamp(float(full_cmp["failed_hypothesis_reduction_percent"]) / 40.0), by_mode[target_mode]["tasks"], f"vs off; {target_mode}")
    else:
        component("failed_hypothesis_reduction", 15, None, 0, "record failed_hypotheses in matched trials")

    if len(task_repositories) >= 2 and known_project_useful >= 3:
        component("cross_project_transfer", 10, 10 * clamp((cross_project_useful / max(1, known_project_useful)) / 0.25), known_project_useful, "saturates at 25% useful cross-project hits")
    else:
        component("cross_project_transfer", 10, None, known_project_useful, "needs >=2 task repositories and >=3 useful records with repository metadata")

    all_devices = task_devices | source_devices
    if len(all_devices) >= 2 and known_device_useful >= 3:
        component("cross_device_transfer", 10, 10 * clamp((cross_device_useful / max(1, known_device_useful)) / 0.25), known_device_useful, "saturates at 25% useful cross-device hits")
    else:
        component("cross_device_transfer", 10, None, known_device_useful, "needs >=2 devices and >=3 useful records with device metadata")

    if recalled_records >= 5:
        component("memory_correctness", 10, 10 * (1.0 - misleading_records / max(1, recalled_records)), recalled_records, "penalizes misleading recalled records")
    else:
        component("memory_correctness", 10, None, recalled_records, "needs >=5 recalled records")

    stale_or_bad = stale_records + misleading_records
    if stale_or_bad:
        component("stale_resistance", 5, 5 * stale_records / stale_or_bad, stale_or_bad, "stale guidance detected rather than trusted")
    else:
        component("stale_resistance", 5, None, 0, "no stale/misleading examples yet")

    component("privacy_integrity", 5, 5.0 if privacy_violations == 0 else 0.0, len(events), "local event schema/path scan")
    if compression_ratio is not None:
        component("decision_compression", 5, 5 * clamp(compression_ratio / 0.90), source_chars, "90% compression reaches full score")
    else:
        component("decision_compression", 5, None, 0, "record source_chars and record_chars")

    available_max = sum(item["max"] for item in components if item["score"] is not None)
    earned = sum(float(item["score"]) for item in components if item["score"] is not None)
    provisional_score_100 = round(earned / available_max * 100.0, 1) if available_max else None
    coverage = round(available_max, 1)
    if coverage < 50:
        status = "insufficient-data"
        score_100 = None
    elif len(completed) < 20:
        status = "provisional"
        score_100 = provisional_score_100
    else:
        status = "established"
        score_100 = provisional_score_100

    return {
        "schema_version": EVAL_SCHEMA_VERSION,
        "generated_at": utc_now(),
        "tracking": load_eval_config(base),
        "metrics": metrics,
        "by_mode": by_mode,
        "comparison": comparison,
        "benchmark": benchmark,
        "scorecard": {"score_100": score_100, "provisional_score_100": provisional_score_100, "coverage_percent": coverage, "status": status, "components": components},
    }


def compact_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    metrics = summary.get("metrics") or {}
    scorecard = summary.get("scorecard") or {}
    benchmark = summary.get("benchmark") or {}
    tracking = summary.get("tracking") or {}
    return {
        "schema_version": 1,
        "enabled": bool(tracking.get("enabled")),
        "mode": str(tracking.get("mode", DEFAULT_MODE)),
        "completed_tasks": int(metrics.get("completed_tasks", 0)),
        "recall_tasks": int(metrics.get("recall_tasks", 0)),
        "useful_task_rate": metrics.get("useful_task_rate"),
        "observed_precision": metrics.get("observed_precision"),
        "mrr_useful": metrics.get("mrr_useful"),
        "cross_project_useful": int(metrics.get("cross_project_useful", 0)),
        "cross_device_useful": int(metrics.get("cross_device_useful", 0)),
        "estimated_saved_seconds": float(metrics.get("estimated_saved_seconds", 0.0)),
        "decision_compression_ratio": metrics.get("decision_compression_ratio"),
        "privacy_violations": int(metrics.get("privacy_violations", 0)),
        "score_100": scorecard.get("score_100"),
        "provisional_score_100": scorecard.get("provisional_score_100"),
        "score_coverage_percent": scorecard.get("coverage_percent"),
        "score_status": scorecard.get("status"),
        "benchmark": {"cases": benchmark.get("cases"), "hit_at_k": benchmark.get("hit_at_k"), "recall_at_k": benchmark.get("recall_at_k"), "mrr": benchmark.get("mrr")} if benchmark else {},
    }


def run_benchmark(base: Path, *, store: Path, suite_path: Path, limit: int, budget: int) -> dict[str, Any]:
    try:
        suite = json.loads(suite_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"could not read benchmark suite: {exc}") from exc
    cases = suite.get("cases") if isinstance(suite, dict) else None
    if not isinstance(cases, list) or not cases:
        raise EvaluationError("benchmark suite requires a non-empty cases array")
    hit = 0
    precision_values: list[float] = []
    recall_values: list[float] = []
    reciprocal: list[float] = []
    for case in cases:
        if not isinstance(case, dict):
            raise EvaluationError("benchmark case must be an object")
        query = str(case.get("query", "")).strip()
        tags = [str(item) for item in case.get("tags", [])]
        expected = {validate_record_id(str(item)) for item in case.get("expected", [])}
        if not query and not tags:
            raise EvaluationError("benchmark case requires query and/or tags")
        if not expected:
            raise EvaluationError("benchmark case requires expected record ids")
        result = recall_records(store, query, tags, limit=limit, budget=budget)
        returned = [str(item.get("id", "")) for item in result.get("matches", [])]
        relevant = [rid for rid in returned if rid in expected]
        if relevant:
            hit += 1
            reciprocal.append(1.0 / (returned.index(relevant[0]) + 1))
        else:
            reciprocal.append(0.0)
        precision_values.append(len(relevant) / max(1, limit))
        recall_values.append(len(relevant) / len(expected))
    result = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "cases": len(cases),
        "k": limit,
        "hit_at_k": round(hit / len(cases), 4),
        "precision_at_k": round(statistics.fmean(precision_values), 4),
        "recall_at_k": round(statistics.fmean(recall_values), 4),
        "mrr": round(statistics.fmean(reciprocal), 4),
    }
    atomic_json(benchmark_last_path(base), result)
    return result


def print_human_summary(summary: Mapping[str, Any]) -> None:
    metrics = summary["metrics"]
    score = summary["scorecard"]
    print("ROUTECRAFT MEMORY EVALUATION")
    print(f"completed tasks: {metrics['completed_tasks']}")
    print(f"recall tasks: {metrics['recall_tasks']}")
    print(f"useful task rate: {metrics['useful_task_rate']}")
    print(f"observed precision: {metrics['observed_precision']}")
    print(f"MRR useful: {metrics['mrr_useful']}")
    print(f"cross-project useful: {metrics['cross_project_useful']}")
    print(f"cross-device useful: {metrics['cross_device_useful']}")
    print(f"estimated saved seconds: {metrics['estimated_saved_seconds']}")
    print(f"score: {score['score_100']} / 100 (coverage {score['coverage_percent']}%, {score['status']})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="routecraft-evaluation", description="Local-only RouteCraft memory effectiveness evaluation")
    parser.add_argument("--dir", help="Override local evaluation directory")
    sub = parser.add_subparsers(dest="command", required=True)
    configure = sub.add_parser("configure", help="Enable/disable evaluation and configure experiment mode")
    toggle = configure.add_mutually_exclusive_group()
    toggle.add_argument("--enable", action="store_true")
    toggle.add_argument("--disable", action="store_true")
    configure.add_argument("--mode", choices=VALID_MODES)
    configure.add_argument("--experiment", choices=("off", "round-robin"))
    configure.add_argument("--sequence", nargs="+", choices=VALID_MODES)
    configure.add_argument("--json", action="store_true")
    start = sub.add_parser("start", help="Start a measured RouteCraft task")
    start.add_argument("--repository")
    start.add_argument("--repo-path")
    start.add_argument("--task-class", choices=VALID_TASK_CLASSES, default="general")
    start.add_argument("--risk", choices=VALID_RISKS, default="low")
    start.add_argument("--mode", choices=VALID_MODES)
    start.add_argument("--json", action="store_true")
    recall = sub.add_parser("recall", help="Record IDs returned by a bounded memory recall")
    recall.add_argument("--task-id", required=True)
    recall.add_argument("--store", required=True)
    recall.add_argument("--record", action="append", default=[], help="RECORD_ID[:RANK], repeatable")
    recall.add_argument("--json", action="store_true")
    finish = sub.add_parser("finish", help="Finish a measured task and classify recalled memory")
    finish.add_argument("--task-id", required=True)
    finish.add_argument("--outcome", choices=VALID_OUTCOMES, required=True)
    finish.add_argument("--elapsed-seconds", type=float)
    finish.add_argument("--tool-calls", type=int)
    finish.add_argument("--failed-hypotheses", type=int)
    finish.add_argument("--useful-record", action="append", default=[])
    finish.add_argument("--misleading-record", action="append", default=[])
    finish.add_argument("--stale-record", action="append", default=[])
    finish.add_argument("--learned-record", action="append", default=[])
    finish.add_argument("--source-chars", type=int)
    finish.add_argument("--record-chars", type=int)
    finish.add_argument("--json", action="store_true")
    summary = sub.add_parser("summary", help="Aggregate local effectiveness metrics and scorecard")
    summary.add_argument("--json", action="store_true")
    summary.add_argument("--compact", action="store_true")
    benchmark = sub.add_parser("benchmark", help="Run a local retrieval benchmark suite")
    benchmark.add_argument("--store", required=True)
    benchmark.add_argument("--suite", required=True)
    benchmark.add_argument("--limit", type=int, default=5)
    benchmark.add_argument("--budget", type=int, default=12000)
    benchmark.add_argument("--json", action="store_true")
    return parser


def parse_ranked(values: Sequence[str]) -> list[tuple[str, int]]:
    ranked: list[tuple[str, int]] = []
    fallback_rank = 1
    for value in values:
        raw = value.strip()
        if ":" in raw:
            rid, rank_text = raw.rsplit(":", 1)
            try:
                rank = int(rank_text)
            except ValueError as exc:
                raise EvaluationError(f"invalid recall rank: {raw!r}") from exc
        else:
            rid, rank = raw, fallback_rank
        if rank < 1 or rank > 100:
            raise EvaluationError("recall rank must be 1..100")
        ranked.append((validate_record_id(rid), rank))
        fallback_rank = rank + 1
    return ranked


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    base = Path(args.dir).expanduser().resolve() if args.dir else default_eval_dir()
    try:
        if args.command == "configure":
            with EvalLock(base):
                config = load_eval_config(base)
                if args.enable:
                    config["enabled"] = True
                if args.disable:
                    config["enabled"] = False
                if args.mode:
                    config["mode"] = args.mode
                if args.experiment:
                    config["experiment"]["enabled"] = args.experiment == "round-robin"
                    config["experiment"]["strategy"] = "round-robin"
                if args.sequence:
                    config["experiment"]["sequence"] = list(args.sequence)
                save_eval_config(base, config)
            output = config
        elif args.command == "start":
            repository = normalize_repository(args.repository or "")
            if not repository and args.repo_path:
                repository = repository_from_path(Path(args.repo_path).expanduser().resolve())
            output = start_task(base, repository=repository, task_class=args.task_class, risk=args.risk, mode_override=args.mode)
        elif args.command == "recall":
            output = record_recall(base, task_id=args.task_id, store=Path(args.store).expanduser().resolve(), ranked_ids=parse_ranked(args.record))
        elif args.command == "finish":
            for value, label in ((args.elapsed_seconds, "elapsed-seconds"), (args.tool_calls, "tool-calls"), (args.failed_hypotheses, "failed-hypotheses"), (args.source_chars, "source-chars"), (args.record_chars, "record-chars")):
                if value is not None and value < 0:
                    raise EvaluationError(f"{label} must be non-negative")
            output = finish_task(base, task_id=args.task_id, outcome=args.outcome, elapsed_seconds=args.elapsed_seconds, tool_calls=args.tool_calls, failed_hypotheses=args.failed_hypotheses, useful_records=args.useful_record, misleading_records=args.misleading_record, stale_records=args.stale_record, learned_records=args.learned_record, source_chars=args.source_chars, record_chars=args.record_chars)
        elif args.command == "summary":
            summary = summarize(base)
            output = compact_summary(summary) if args.compact else summary
            if not args.json:
                print_human_summary(summary)
                return 0
        elif args.command == "benchmark":
            output = run_benchmark(base, store=Path(args.store).expanduser().resolve(), suite_path=Path(args.suite).expanduser().resolve(), limit=args.limit, budget=args.budget)
        else:
            parser.error("unknown command")
            return 2
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except EvaluationError as exc:
        print(f"routecraft-evaluation: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
