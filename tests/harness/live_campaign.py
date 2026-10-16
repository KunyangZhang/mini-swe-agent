"""Real greedy local model runs; preserve failures and score outside the agent loop."""

import argparse
import hashlib
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

from tests.harness.coding_campaign import state, write_json
from tests.harness.coding_worker import FIXTURE


def invoke(command: list[str], workspace: Path, label: str, fault: str = "", cwd: Path | None = None) -> dict:
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    start = time.monotonic()
    with (
        (workspace / f"{label}.stdout.txt").open("w") as stdout,
        (workspace / f"{label}.stderr.txt").open("w") as stderr,
    ):
        result = subprocess.run(
            command,
            stdout=stdout,
            stderr=stderr,
            cwd=cwd,
            timeout=720,
            env=os.environ | {"MSWEA_SILENT_STARTUP": "1", "CODING_FAULT": fault},
        )
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
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


def run(output: Path, model: Path, include_source: bool = False) -> dict:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    for name, baseline, fault in [
        ("default_uninterrupted", True, ""),
        ("durable_uninterrupted", False, ""),
        ("durable_after_reply", False, "after_reply"),
    ]:
        workspace = output / name
        workspace.mkdir()
        for source in FIXTURE.glob("*.txt"):
            (workspace / source.stem).write_bytes(source.read_bytes())
        test_hash = hashlib.sha256((workspace / "test_chunking.py").read_bytes()).hexdigest()
        command = [
            sys.executable,
            "-m",
            "tests.harness.live_model",
            str(workspace),
            "--model-path",
            str(model.resolve()),
        ]
        if baseline:
            command.append("--baseline")
        if include_source:
            command.append("--include-source")
        first = invoke(command, workspace, "initial-run", fault)
        write_json(workspace / "after-first-run.json", state(workspace))
        resumed = invoke(command, workspace, "resume") if first["returncode"] == 77 else None
        acceptance = invoke(
            [sys.executable, "-m", "pytest", "-q", "test_chunking.py"], workspace, "acceptance", cwd=workspace
        )
        trajectory = (
            json.loads((workspace / "trajectory.json").read_text()) if (workspace / "trajectory.json").exists() else {}
        )
        requests = (
            [json.loads(line) for line in (workspace / "model-requests.jsonl").read_text().splitlines()]
            if (workspace / "model-requests.jsonl").exists()
            else []
        )
        tools = (
            [json.loads(line) for line in (workspace / "tool-executions.jsonl").read_text().splitlines()]
            if (workspace / "tool-executions.jsonl").exists()
            else []
        )
        test_unchanged = hashlib.sha256((workspace / "test_chunking.py").read_bytes()).hexdigest() == test_hash
        submitted = trajectory.get("info", {}).get("exit_status") == "Submitted"
        row = {
            "case": name,
            "initial_returncode": first["returncode"],
            "resume_returncode": resumed["returncode"] if resumed else None,
            "agent_exit_status": trajectory.get("info", {}).get("exit_status"),
            "agent_submitted": submitted,
            "acceptance_passed": acceptance["returncode"] == 0,
            "test_file_unchanged": test_unchanged,
            "task_success": acceptance["returncode"] == 0 and submitted and test_unchanged,
            "actual_model_calls": len(requests),
            "actual_tool_executions": len(tools),
            "input_tokens": sum(r["usage"]["input_tokens"] for r in requests),
            "output_tokens": sum(r["usage"]["output_tokens"] for r in requests),
            "generation_seconds": sum(r["usage"]["generation_seconds"] for r in requests),
            "peak_cuda_allocated_bytes": max((r["usage"]["peak_cuda_allocated_bytes"] for r in requests), default=0),
            "api_cost_usd": None,
            "gpu_rental_cost": None,
            "task_source_changed": (workspace / "chunking.py").read_bytes()
            != (FIXTURE / "chunking.py.txt").read_bytes(),
        }
        write_json(workspace / "final-state.json", state(workspace))
        write_json(workspace / "result.json", row)
        rows.append(row)
        write_json(
            output / "report.json",
            {
                "model": "Qwen/Qwen2.5-Coder-3B-Instruct",
                "model_path": str(model),
                "scope": "single tiny coding task, greedy local inference; integration smoke test, not benchmark/pass-rate evidence",
                "initial_source_in_prompt": include_source,
                "cases": rows,
            },
        )
        print(json.dumps(row), flush=True)
    return {"cases": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--include-source", action="store_true")
    args = parser.parse_args()
    run(args.output, args.model_path, args.include_source)
