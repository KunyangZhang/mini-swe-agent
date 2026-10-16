"""Real coding task resumed from a different launcher working directory."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from minisweagent.agents.durable import DurableAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.harness.replay import ReplayModel
from minisweagent.harness.store import RunStore
from tests.harness.coding_worker import FIXTURE, TASK, CodingStore, append, tape


class CountingEnvironment(LocalEnvironment):
    def execute(self, action: dict, cwd: str = "", **kwargs) -> dict:
        append(Path(cwd or self.config.cwd or os.getcwd()) / "executions.jsonl", action)
        return super().execute(action, cwd, **kwargs)


def worker(directory: Path, mode: str) -> None:
    with CodingStore(directory / "run") as store:
        agent = DurableAgent(
            ReplayModel(outputs=tape(), cost_per_call=0),
            CountingEnvironment(cwd={"default": "", "relative": ".", "absolute": str(directory / "original")}[mode]),
            store=store,
            system_template="Scripted workspace binding test",
            instance_template="{{task}}",
            cost_limit=0,
            output_path=directory / "trajectory.json",
        )
        print(json.dumps(agent.run(TASK)))


def count(path: Path) -> int:
    return len(path.read_text().splitlines()) if path.exists() else 0


def run_case(directory: Path, mode: str) -> dict:
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    for name in ("original", "other"):
        workspace = directory / name
        workspace.mkdir()
        for source in FIXTURE.glob("*.txt"):
            (workspace / source.stem).write_bytes(source.read_bytes())
    command = [sys.executable, "-m", "tests.harness.workspace_campaign", "--worker", str(directory), "--mode", mode]
    env = os.environ | {
        "PYTHONPATH": os.pathsep.join(
            str(Path(entry or ".").resolve()) for entry in os.getenv("PYTHONPATH", "").split(os.pathsep)
        )
        + os.pathsep
        + str(Path(__file__).resolve().parents[2] / "src")
        + os.pathsep
        + str(Path(__file__).resolve().parents[2]),
        "MSWEA_SILENT_STARTUP": "1",
    }
    processes = {}
    for label, cwd, fault in (
        ("interruption", "original", "after_reply"),
        ("changed-launcher", "other", ""),
        ("original-launcher", "original", ""),
    ):
        started = time.monotonic()
        result = subprocess.run(
            command, cwd=directory / cwd, env=env | {"CODING_FAULT": fault}, text=True, capture_output=True, timeout=60
        )
        (directory / f"{label}.stdout.txt").write_text(result.stdout)
        (directory / f"{label}.stderr.txt").write_text(result.stderr)
        with RunStore(directory / "run") as store:
            (directory / f"{label}.state.json").write_text(json.dumps(store.status(), indent=2))
        processes[label] = {
            "returncode": result.returncode,
            "seconds": time.monotonic() - started,
            "command": command,
            "cwd": str(directory / cwd),
            "rejected_binding": "Run configuration or task changed" in result.stderr,
        }
    accepted = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "test_chunking.py"],
        cwd=directory / "original",
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    (directory / "acceptance.txt").write_text(accepted.stdout + accepted.stderr)
    report = {
        "mode": mode,
        "processes": processes,
        "original_task_passed": accepted.returncode == 0,
        "original_tool_executions": count(directory / "original/executions.jsonl"),
        "other_tool_executions": count(directory / "other/executions.jsonl"),
        "original_patch_writes": count(directory / "original/patch-ledger.txt"),
        "other_patch_writes": count(directory / "other/patch-ledger.txt"),
        "api_cost_usd": None,
        "model": "scripted ReplayModel",
    }
    (directory / "report.json").write_text(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--mode", choices=["default", "relative", "absolute"])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.worker:
        worker(args.worker, args.mode)
    else:
        args.output.mkdir(parents=True, exist_ok=False)
        rows = [run_case(args.output / mode, mode) for mode in ("default", "relative", "absolute")]
        (args.output / "report.json").write_text(json.dumps(rows, indent=2))
        print(json.dumps(rows, indent=2))
