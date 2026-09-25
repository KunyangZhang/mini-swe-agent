import json
import os
import subprocess
import sys


def invoke(*args):
    return subprocess.run(
        [sys.executable, "-m", "minisweagent.run.extra.durable", *map(str, args)],
        env=os.environ | {"MSWEA_SILENT_STARTUP": "1"},
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_cli_replays_real_commands_exports_and_resumes(tmp_path):
    tape = tmp_path / "tape.json"
    tape.write_text(
        json.dumps(
            [
                {
                    "role": "assistant",
                    "content": "write",
                    "extra": {"cost": 0, "actions": [{"command": "echo proof >> proof.txt"}]},
                },
                {
                    "role": "assistant",
                    "content": "finish",
                    "extra": {"cost": 0, "actions": [{"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}]},
                },
            ]
        )
    )
    args = ("run", tmp_path / "session", "--task", "demo", "--workspace", tmp_path, "--replay", tape)
    result = invoke(*args)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["exit_status"] == "Submitted"
    assert invoke(*args).returncode == 0
    assert (tmp_path / "proof.txt").read_text() == "proof\n"
    status = invoke("inspect", tmp_path / "session")
    assert status.returncode == 0 and json.loads(status.stdout)["model_calls"] == 2
    assert (tmp_path / "session" / "trajectory.json").is_file()


def test_cli_rejects_bad_provider_selection_and_missing_journal(tmp_path):
    result = invoke("run", tmp_path / "session", "--task", "demo", "--workspace", tmp_path)
    assert result.returncode != 0 and "Choose exactly one" in result.stderr
    assert invoke("inspect", tmp_path / "missing").returncode != 0
    assert not (tmp_path / "missing").exists()


def test_cli_returns_nonzero_on_step_limit(tmp_path):
    tape = tmp_path / "tape.json"
    tape.write_text(
        json.dumps([{"role": "assistant", "content": "loop", "extra": {"cost": 0, "actions": [{"command": "echo x"}]}}])
    )
    result = invoke(
        "run", tmp_path / "session", "--task", "demo", "--workspace", tmp_path, "--replay", tape, "--steps", 1
    )
    assert result.returncode == 2 and json.loads(result.stdout)["exit_status"] == "LimitsExceeded"
