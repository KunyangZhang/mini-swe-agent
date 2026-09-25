"""Reproducible process-crash experiment. No model capability claims or API calls."""

import json
import os
import platform
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import typer

from minisweagent.harness.store import RunStore

app = typer.Typer()


def invoke(workspace: Path, crash: str = "", baseline: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "tests.harness.worker", str(workspace)],
        capture_output=True,
        text=True,
        timeout=20,
        env=os.environ
        | {"HARNESS_CRASH": crash, "HARNESS_BASELINE": "1" if baseline else "", "MSWEA_SILENT_STARTUP": "1"},
    )


@app.command()
def main(output: Path = Path("evidence/recovery-report.json")) -> None:
    rows = []
    with tempfile.TemporaryDirectory(prefix="harness-eval-") as temporary:
        root = Path(temporary)
        for mode in [
            "after_reply",
            "before_begin",
            "after_result",
            "after_observation",
            "before_effect",
            "after_effect",
            "during_query",
        ]:
            workspace = root / mode
            workspace.mkdir()
            killed = invoke(workspace, mode)
            resumed = invoke(workspace)
            assert killed.returncode == 77
            expected_block = mode in {"before_effect", "after_effect", "during_query"}
            assert (resumed.returncode != 0) == expected_block, resumed.stderr
            counter = workspace / "counter"
            writes = len(counter.read_text().splitlines()) if counter.exists() else 0
            assert writes <= 1
            row = {
                "crash_point": mode,
                "automatic_resume": "blocked" if expected_block else "completed",
                "writes_before_resolution": writes,
            }
            if mode in {"before_effect", "after_effect"}:
                with RunStore(workspace / "run") as store:
                    store.resolve(
                        1,
                        0,
                        {"output": "", "returncode": 0},
                        "Evaluator inspected the counter; accepts observed outcome",
                    )
                assert invoke(workspace).returncode == 0
                row["after_verified_resolution"] = "completed"
            rows.append(row)
        baseline = root / "baseline"
        baseline.mkdir()
        assert invoke(baseline, "after_effect", baseline=True).returncode == 77
        assert invoke(baseline, baseline=True).returncode == 0
        baseline_writes = len((baseline / "counter").read_text().splitlines())
        assert baseline_writes == 2
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "upstream_commit": "04d809ceab9df28f9adaed044884180159172930",
        "evaluation_type": "scripted model, real local commands and abrupt child-process exits; not an LLM benchmark",
        "cases": rows,
        "baseline": {
            "policy": "unmodified DefaultAgent, fresh run after crash",
            "crash_point": "after_effect",
            "writes": baseline_writes,
        },
        "limits": [
            "No exactly-once guarantee for external effects",
            "Model in-flight uncertainty stops the run",
            "No SWE-bench or live-model quality measurement",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    app()
