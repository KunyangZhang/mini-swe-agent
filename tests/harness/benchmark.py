"""Repeated real process faults; measures recovery policy, not LLM task quality."""

import argparse
import json
import platform
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from tests.harness.evaluate import invoke

MODES = (
    "after_reply",
    "before_begin",
    "after_result",
    "after_observation",
    "before_effect",
    "after_effect",
    "during_query",
)
BLOCKED = {"before_effect", "after_effect", "during_query"}


def measure(item):
    root, mode, repeat, baseline = item
    workspace = root / f"{'baseline' if baseline else 'durable'}-{mode}-{repeat}"
    workspace.mkdir()
    killed = invoke(workspace, mode, baseline)
    started = time.perf_counter()
    resumed = invoke(workspace, baseline=baseline)
    elapsed = time.perf_counter() - started
    counter = workspace / "counter"
    writes = len(counter.read_text().splitlines()) if counter.exists() else 0
    expected_block = not baseline and mode in BLOCKED
    if killed.returncode != 77 or (resumed.returncode != 0) != expected_block:
        raise RuntimeError(f"unexpected recovery at {mode}: {killed.returncode}/{resumed.returncode}: {resumed.stderr}")
    if (baseline and writes != 2) or (not baseline and writes > 1):
        raise RuntimeError(f"effect invariant failed at {mode}: writes={writes}")
    if not baseline and not expected_block and writes != 1:
        raise RuntimeError("automatic recovery must complete exactly one observable write")
    return {
        "policy": "fresh_restart" if baseline else "durable",
        "fault": mode,
        "repeat": repeat,
        "outcome": "blocked" if expected_block else "completed",
        "effect_writes": writes,
        "resume_seconds_including_process_start": elapsed,
    }


def run(repeats, workers, output):
    if repeats < 1 or workers < 1:
        raise ValueError("positive repeats/workers required")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="harness-bulk-") as d:
        root = Path(d)
        jobs = [(root, mode, i, False) for i in range(repeats) for mode in MODES]
        jobs += [(root, "after_effect", i, True) for i in range(repeats)]
        with ThreadPoolExecutor(workers) as pool:
            rows = list(pool.map(measure, jobs))
    durable = [r for r in rows if r["policy"] == "durable"]
    baseline = [r for r in rows if r["policy"] == "fresh_restart"]
    grouped = {}
    for mode in MODES:
        cases = [r for r in durable if r["fault"] == mode]
        grouped[mode] = {
            "n": len(cases),
            "outcomes": dict(Counter(r["outcome"] for r in cases)),
            "duplicate_effect_cases": sum(r["effect_writes"] > 1 for r in cases),
        }
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "repeats_per_fault": repeats,
        "workers": workers,
        "seconds": time.monotonic() - started,
        "scope": "scripted model; real local subprocess commands; abrupt os._exit(77) at seven transaction boundaries",
        "summary": {
            "durable_cases": len(durable),
            "automatic_completed": sum(r["outcome"] == "completed" for r in durable),
            "safely_blocked": sum(r["outcome"] == "blocked" for r in durable),
            "duplicate_effect_cases": sum(r["effect_writes"] > 1 for r in durable),
            "fresh_restart_cases": len(baseline),
            "fresh_restart_duplicate_cases": sum(r["effect_writes"] > 1 for r in baseline),
        },
        "by_fault": grouped,
        "cases": rows,
        "limits": [
            "Repeats cover the same seven fault boundaries, not 175 distinct scenarios.",
            "Blocked runs require operator reconciliation and are not successful task completions.",
            "Fresh restart is unmodified DefaultAgent; does not represent every competing harness.",
            "No external exactly-once guarantee, machine power-loss simulation, live model or SWE-bench evaluation.",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=25)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output", type=Path, default=Path("evidence/repeated-recovery.json"))
    args = parser.parse_args()
    run(args.repeats, args.workers, args.output)
