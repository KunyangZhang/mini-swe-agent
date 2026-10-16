"""Recovery boundaries beyond the original seven-point counter campaign."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from minisweagent.agents.durable import DurableAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.exceptions import FormatError
from minisweagent.harness.replay import ReplayModel
from minisweagent.harness.store import AmbiguousActionError, RunStore
from tests.harness.coding_worker import FIXTURE
from tests.harness.test_durable import reply


class RecoverableFormatModel(ReplayModel):
    def query(self, messages: list[dict], **kwargs) -> dict:
        count = sum(m.get("role") == "assistant" for m in messages)
        if count in (0, 2):
            raise FormatError({"role": "assistant", "content": "malformed", "extra": {"cost": 0}})
        return reply(["echo clean >> clean" if count == 1 else "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"], cost=0)


class BoundaryStore(RunStore):
    def checkpoint(self, state: dict, kind: str = "checkpoint") -> None:
        super().checkpoint(state, kind)
        if os.getenv("BOUNDARY_FAULT") == "after_clean" and state["phase"] == "ready" and state["n_calls"] == 2:
            os._exit(77)
        if os.getenv("BOUNDARY_FAULT") == "after_reply" and state["phase"] == "executing":
            os._exit(77)


def worker(directory: Path, case: str) -> None:
    with BoundaryStore(directory / "run") as store:
        provider = (
            RecoverableFormatModel(outputs=[])
            if case == "format"
            else ReplayModel(
                outputs=[reply(["echo forbidden >> effects", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"], cost=0)]
            )
        )
        agent = DurableAgent(
            provider,
            LocalEnvironment(cwd=str(directory)),
            store=store,
            system_template="test",
            instance_template="{{task}}",
            cost_limit=0,
            max_consecutive_format_errors=2,
            wall_time_limit_seconds=1 if case == "deadline" else 0,
            output_path=directory / "trajectory.json",
        )
        print(agent.run("boundary"))


def invoke(directory: Path, case: str, fault: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "tests.harness.test_boundaries", str(directory), case],
        capture_output=True,
        text=True,
        timeout=60,
        env=os.environ | {"BOUNDARY_FAULT": fault, "MSWEA_SILENT_STARTUP": "1"},
    )


def test_successful_batch_resets_format_error_count_before_checkpoint(tmp_path):
    assert invoke(tmp_path, "format", "after_clean").returncode == 77
    resumed = invoke(tmp_path, "format")
    assert resumed.returncode == 0, resumed.stderr
    assert "Submitted" in resumed.stdout, resumed.stdout
    assert (tmp_path / "clean").read_text() == "clean\n"
    with RunStore(tmp_path / "run") as store:
        assert store.load()["n_calls"] == 4


def test_expired_pending_reply_does_not_start_shell_after_restart(tmp_path):
    assert invoke(tmp_path, "deadline", "after_reply").returncode == 77
    time.sleep(1.1)
    resumed = invoke(tmp_path, "deadline")
    assert resumed.returncode == 0, resumed.stderr
    assert "TimeExceeded" in resumed.stdout, resumed.stdout
    assert not (tmp_path / "effects").exists()
    with RunStore(tmp_path / "run") as store:
        assert store.status()["actions"] == []
        assert store.load()["n_calls"] == 1


def test_expired_mid_batch_does_not_start_next_action(tmp_path):
    with RunStore(tmp_path / "run") as store:
        agent = DurableAgent(
            ReplayModel(outputs=[reply(["echo first >> effects; sleep 1.1", "echo forbidden >> effects"], cost=0)]),
            LocalEnvironment(cwd=str(tmp_path)),
            store=store,
            system_template="test",
            instance_template="{{task}}",
            cost_limit=0,
            wall_time_limit_seconds=1,
        )
        assert agent.run("deadline")["exit_status"] == "TimeExceeded"
        assert (tmp_path / "effects").read_text() == "first\n"
        assert len(store.status()["actions"]) == 1
        assert store.status()["actions"][0]["result"] is not None


@pytest.mark.parametrize(("resolved",), [(False,), (True,)])  # noqa: PT006
def test_expiry_preserves_unknown_outcomes_and_reuses_recorded_results(tmp_path, resolved):
    with RunStore(tmp_path / "run") as store:
        action = {"command": "echo must-not-run >> effects"}
        store.begin(1, 0, action)
        if resolved:
            store.resolve(1, 0, {"output": "verified", "returncode": 0}, "checked external outcome")
        agent = DurableAgent(
            ReplayModel(outputs=[]),
            LocalEnvironment(cwd=str(tmp_path)),
            store=store,
            system_template="test",
            instance_template="{{task}}",
            wall_time_limit_seconds=1,
        )
        agent.n_calls = 1
        agent._start_time = 0
        if resolved:
            assert "verified" in agent.execute_actions(reply([action["command"]], cost=0))[0]["content"]
        else:
            with pytest.raises(AmbiguousActionError):
                agent.execute_actions(reply([action["command"]], cost=0))
        assert not (tmp_path / "effects").exists()
        assert len(store.status()["actions"]) == 1


def test_competing_resumes_cannot_both_execute_pending_batch(tmp_path):
    for source in FIXTURE.glob("*.txt"):
        (tmp_path / source.stem).write_bytes(source.read_bytes())
    command = [sys.executable, "-m", "tests.harness.coding_worker", str(tmp_path)]
    env = os.environ | {"MSWEA_SILENT_STARTUP": "1", "CODING_BASELINE": "", "CODING_FAULT": "after_reply"}
    assert subprocess.run(command, env=env, capture_output=True, timeout=60).returncode == 77
    first = subprocess.Popen(
        command,
        env=env | {"CODING_FAULT": "", "CODING_GATE": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while not (tmp_path / "gate-ready").exists():
            assert first.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.02)
        second = subprocess.run(command, env=env | {"CODING_FAULT": ""}, capture_output=True, text=True, timeout=30)
        assert second.returncode != 0 and "BlockingIOError" in second.stderr
    finally:
        (tmp_path / "gate-release").touch()
        stdout, stderr = first.communicate(timeout=60)
    assert first.returncode == 0 and "Submitted" in stdout, stderr
    assert (tmp_path / "patch-ledger.txt").read_text() == "patch\n"
    assert len((tmp_path / "tool-executions.jsonl").read_text().splitlines()) == 4


if __name__ == "__main__":
    worker(Path(sys.argv[1]), sys.argv[2])
