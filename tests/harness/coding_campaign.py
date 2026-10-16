"""Preserve coding artifacts, raw logs, resources and independent acceptance results."""

import argparse
import hashlib
import json
import os
import platform
import resource
import subprocess
import sys
import time
from pathlib import Path

from minisweagent.harness.store import RunStore
from tests.harness.coding_worker import FIXTURE, SOLUTION


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def invoke(workspace: Path, label: str, fault: str = "", baseline: bool = False, acceptance: bool = False) -> dict:
    command = (
        [sys.executable, "-m", "pytest", "-q", "test_chunking.py"]
        if acceptance
        else [sys.executable, "-m", "tests.harness.coding_worker", str(workspace)]
    )
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    start = time.monotonic()
    result = subprocess.run(
        command,
        cwd=workspace if acceptance else None,
        capture_output=True,
        text=True,
        timeout=90,
        env=os.environ
        | {"CODING_FAULT": fault, "CODING_BASELINE": "1" if baseline else "", "MSWEA_SILENT_STARTUP": "1"},
    )
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    (workspace / f"{label}.stdout.txt").write_text(result.stdout)
    (workspace / f"{label}.stderr.txt").write_text(result.stderr)
    measurement = {
        "command": command,
        "fault": fault,
        "returncode": result.returncode,
        "wall_seconds": time.monotonic() - start,
        "cpu_user_seconds": after.ru_utime - before.ru_utime,
        "cpu_system_seconds": after.ru_stime - before.ru_stime,
        "children_maxrss_kib_cumulative": after.ru_maxrss,
    }
    write_json(workspace / f"{label}.process.json", measurement)
    return measurement


def state(workspace: Path) -> dict:
    with RunStore(workspace / "run") as store:
        status = store.status()
        status["snapshot"] = store.load()
    status["files"] = {
        name: {
            "sha256": hashlib.sha256((workspace / name).read_bytes()).hexdigest(),
            "content": (workspace / name).read_text(),
        }
        for name in ("chunking.py", "patch-ledger.txt")
        if (workspace / name).exists()
    }
    return status


def lines(path: Path) -> int:
    return len(path.read_text().splitlines()) if path.exists() else 0


def run(output: Path) -> dict:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    cases = [
        ("uninterrupted", "", False),
        ("batch_after_patch_result", "after_patch_result", False),
        ("before_observation_checkpoint", "before_observation_checkpoint", False),
        ("manual_after_patch_effect", "after_patch_effect", False),
        ("fresh_restart_after_patch_effect", "after_patch_effect", True),
        ("workspace_drift_negative", "before_observation_checkpoint", False),
    ]
    for name, fault, baseline in cases:
        workspace = output / name
        workspace.mkdir()
        for source in FIXTURE.glob("*.txt"):
            (workspace / source.stem).write_bytes(source.read_bytes())
        original_test_hash = hashlib.sha256((workspace / "test_chunking.py").read_bytes()).hexdigest()
        initial = invoke(workspace, "initial-acceptance", acceptance=True)
        assert initial["returncode"] == 1
        first = invoke(workspace, "initial-run", fault, baseline)
        assert first["returncode"] == (77 if fault else 0)
        write_json(workspace / "after-interruption.json", state(workspace))
        if name == "workspace_drift_negative":
            (workspace / "chunking.py").write_bytes((FIXTURE / "chunking.py.txt").read_bytes())
            write_json(workspace / "external-drift.json", state(workspace))
        resumed = invoke(workspace, "automatic-resume", baseline=baseline)
        automatic = resumed["returncode"] == 0
        if name == "manual_after_patch_effect":
            assert not automatic and "AmbiguousActionError" in (workspace / "automatic-resume.stderr.txt").read_text()
            assert lines(workspace / "patch-ledger.txt") == 1
            assert (workspace / "chunking.py").read_text() == SOLUTION
            assert invoke(workspace, "independent-verification", acceptance=True)["returncode"] == 0
            reason = "Child exited 77; checked exact source, one ledger entry and independent 8-test pytest success."
            with RunStore(workspace / "run") as store:
                store.resolve(1, 1, {"output": "", "returncode": 0}, reason)
            write_json(workspace / "operator-verification.json", {"reason": reason, "simulated_operator": True})
            assert invoke(workspace, "verified-resume")["returncode"] == 0
        else:
            assert automatic
        final = invoke(workspace, "final-acceptance", acceptance=True)
        assert hashlib.sha256((workspace / "test_chunking.py").read_bytes()).hexdigest() == original_test_hash
        snapshot = state(workspace)
        write_json(workspace / "final-state.json", snapshot)
        executions = lines(workspace / "tool-executions.jsonl")
        writes = lines(workspace / "patch-ledger.txt")
        trajectory = json.loads((workspace / "trajectory.json").read_text())
        submitted = trajectory["info"]["exit_status"] == "Submitted"
        row = {
            "case": name,
            "policy": "fresh DefaultAgent restart" if baseline else "durable",
            "automatic_completed": automatic and final["returncode"] == 0 and submitted,
            "required_verified_resolution": name == "manual_after_patch_effect",
            "final_task_success": final["returncode"] == 0 and submitted,
            "agent_submitted": submitted,
            "actual_tool_executions": executions,
            "actual_scripted_model_calls": lines(workspace / "model-calls.jsonl"),
            "patch_writes": writes,
            "duplicate_patch_writes": max(0, writes - 1),
            "api_calls": 0,
            "api_cost_usd": None,
            "test_file_unchanged": True,
        }
        assert row["final_task_success"] == (name != "workspace_drift_negative")
        assert writes == (2 if baseline else 1)
        if not baseline:
            assert executions == 4
            assert invoke(workspace, "terminal-resume")["returncode"] == 0
            assert lines(workspace / "tool-executions.jsonl") == executions
        write_json(workspace / "result.json", row)
        rows.append(row)
    report = {
        "scope": "scripted mechanism experiment; actual Python edits and pytest; no live model/API",
        "python": platform.python_version(),
        "cases": rows,
        "resource_notes": "Per invocation wall/CPU timings; maxrss is cumulative child high-water mark, not per-case peak.",
    }
    write_json(output / "report.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(run(parser.parse_args().output), indent=2))
