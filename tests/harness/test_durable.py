import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from minisweagent.agents.durable import DurableAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.exceptions import FormatError
from minisweagent.harness.replay import ReplayModel
from minisweagent.harness.store import AmbiguousActionError, RunStore


def reply(commands: list[str], cost: float = 0.125) -> dict:
    return {
        "role": "assistant",
        "content": "scripted",
        "extra": {
            "cost": cost,
            "actions": [{"command": command} for command in commands],
        },
    }


def model(*commands: str) -> ReplayModel:
    return ReplayModel(outputs=[reply([c]) for c in commands])


def agent(store: RunStore, provider: ReplayModel, **kwargs) -> DurableAgent:
    return DurableAgent(
        provider,
        LocalEnvironment(cwd=str(store.directory)),
        store=store,
        **(
            {
                "system_template": "test",
                "instance_template": "{{task}}",
                "cost_limit": 2,
            }
            | kwargs
        ),
    )


def child(directory: Path, crash: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "tests.harness.worker", str(directory)],
        env=os.environ | {"HARNESS_CRASH": crash, "MSWEA_SILENT_STARTUP": "1"},
        capture_output=True,
        text=True,
        timeout=20,
    )


@pytest.mark.parametrize(("crash",), [("after_reply",), ("before_begin",), ("after_result",), ("after_observation",)])  # noqa: PT006 - repository requires tuple names
def test_process_death_resumes_without_duplicate_effect(tmp_path, crash):
    assert child(tmp_path, crash).returncode == 77
    resumed = child(tmp_path)
    assert resumed.returncode == 0, resumed.stderr
    assert (tmp_path / "counter").read_text() == "one\n"
    trajectory = json.loads((tmp_path / "trajectory.json").read_text())
    assert trajectory["info"]["exit_status"] == "Submitted"
    assert trajectory["info"]["model_stats"] == {"api_calls": 2, "instance_cost": 0.25}
    assert child(tmp_path).returncode == 0
    assert (tmp_path / "counter").read_text() == "one\n"


@pytest.mark.parametrize(("crash", "side_effect"), [("before_effect", False), ("after_effect", True)])
def test_uncertain_action_blocks_until_explicit_resolution(tmp_path, crash, side_effect):
    assert child(tmp_path, crash).returncode == 77
    assert (tmp_path / "counter").exists() == side_effect
    resumed = child(tmp_path)
    assert resumed.returncode != 0 and "AmbiguousActionError" in resumed.stderr
    assert (tmp_path / "counter").exists() == side_effect
    with RunStore(tmp_path / "run") as store:
        store.resolve(1, 0, {"output": "", "returncode": 0}, "Inspected counter; accept observed outcome")
    assert child(tmp_path).returncode == 0
    assert (tmp_path / "counter").exists() == side_effect
    if side_effect:
        assert (tmp_path / "counter").read_text() == "one\n"


def test_unknown_model_request_is_not_silently_reissued(tmp_path):
    assert child(tmp_path, "during_query").returncode == 77
    resumed = child(tmp_path)
    assert resumed.returncode != 0 and "Model request outcome is unknown" in resumed.stderr
    assert not (tmp_path / "counter").exists()


def test_repeated_identical_commands_at_different_positions_are_not_deduplicated(tmp_path):
    with RunStore(tmp_path) as store:
        result = agent(store, model("echo x >> count", "echo x >> count", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"))
        assert result.run("test")["exit_status"] == "Submitted"
        assert (tmp_path / "count").read_text() == "x\nx\n"
        assert len([e for e in store.status()["events"] if e["kind"] == "action_completed"]) == 2


@pytest.mark.parametrize(("limits", "calls"), [({"step_limit": 1}, 1), ({"cost_limit": 0.1}, 1)])
def test_budget_persists_and_stops_future_queries(tmp_path, limits, calls):
    with RunStore(tmp_path) as store:
        provider = model("echo once >> count", "echo twice >> count")
        first = agent(store, provider, **limits)
        assert first.run("test")["exit_status"] == "LimitsExceeded"
        assert (
            agent(store, model("echo once >> count", "echo twice >> count"), **limits).run("test")
            == first.messages[-1]["extra"]
        )
        assert store.load()["n_calls"] == calls
        assert (tmp_path / "count").read_text() == "once\n"


def test_large_unicode_output_has_full_artifact_and_bounded_observation(tmp_path):
    with RunStore(tmp_path) as store:
        runner = agent(
            store,
            model("python3 -c \"print('汉' * 4000)\"", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
            observation_chars=256,
        )
        assert runner.run("test")["exit_status"] == "Submitted"
        artifacts = list((tmp_path / "artifacts").glob("*.txt"))
        assert len(artifacts) == 1 and artifacts[0].read_text() == "汉" * 4000 + "\n"
        assert "Output truncated" in runner.messages[3]["content"]
        assert len(runner.messages[3]["content"]) < 1000


def test_multiple_actions_and_nonzero_exit_preserve_observation_order(tmp_path):
    provider = ReplayModel(
        outputs=[reply(["echo first", "echo second; exit 7"]), reply(["echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"])]
    )
    with RunStore(tmp_path) as store:
        runner = agent(store, provider)
        assert runner.run("test")["exit_status"] == "Submitted"
        assert "first" in runner.messages[3]["content"]
        assert (
            "second" in runner.messages[4]["content"] and "<returncode>7</returncode>" in runner.messages[4]["content"]
        )


@pytest.mark.parametrize(("changed",), [("task",), ("model",), ("environment",), ("budget",)])  # noqa: PT006 - repository requires tuple names
def test_resume_rejects_changed_identity(tmp_path, changed):
    with RunStore(tmp_path) as store:
        runner = agent(store, model("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"))
        runner.run("test")
        provider = model("echo changed") if changed == "model" else model("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")
        second = agent(store, provider, **({"step_limit": 10} if changed == "budget" else {}))
        if changed == "environment":
            second = DurableAgent(
                provider,
                LocalEnvironment(cwd="/tmp"),
                store=store,
                system_template="test",
                instance_template="{{task}}",
                cost_limit=2,
            )
        with pytest.raises(ValueError, match="configuration or task changed"):
            second.run("other" if changed == "task" else "test")


def test_journal_identity_resolution_and_invalid_transitions(tmp_path):
    with RunStore(tmp_path) as store:
        assert store.begin(1, 0, {"command": "echo ok"}) is None
        with pytest.raises(AmbiguousActionError):
            store.begin(1, 0, {"command": "echo ok"})
        with pytest.raises(ValueError, match="identity"):
            store.begin(1, 0, {"command": "echo changed"})
        with pytest.raises(ValueError, match="reason"):
            store.resolve(1, 0, {"output": "", "returncode": 0}, "")
        store.resolve(1, 0, {"output": "ok\n", "returncode": 0}, "verified filesystem")
        assert store.begin(1, 0, {"command": "echo ok"}) == {"output": "ok\n", "returncode": 0}
        with pytest.raises(ValueError, match="unresolved"):
            store.resolve(1, 0, {"output": "", "returncode": 0}, "again")
        with pytest.raises(ValueError, match="already completed"):
            store.complete(1, 0, {})


def test_second_writer_cannot_start_and_lock_is_released(tmp_path):
    with RunStore(tmp_path):
        process = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; from minisweagent.harness.store import RunStore; RunStore(Path(__import__('sys').argv[1])).__enter__()",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert process.returncode != 0 and "BlockingIOError" in process.stderr
    with RunStore(tmp_path) as store:
        assert store.load() is None


def test_unknown_schema_fails_without_leaking_lock(tmp_path):
    with RunStore(tmp_path) as store:
        store.db.execute("PRAGMA user_version=99")
    for _ in range(2):
        with pytest.raises(ValueError, match="Unsupported journal version"), RunStore(tmp_path):
            pass


def test_timeout_is_recorded_as_result_and_not_retried(tmp_path):
    with RunStore(tmp_path) as store:
        runner = DurableAgent(
            model("echo started >> counter; sleep 3", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
            LocalEnvironment(cwd=str(tmp_path), timeout=1),
            store=store,
            system_template="test",
            instance_template="{{task}}",
            cost_limit=2,
        )
        assert runner.run("test")["exit_status"] == "Submitted"
        assert (tmp_path / "counter").read_text() == "started\n"
        assert json.loads(store.status()["actions"][0]["result"])["extra"]["exception_type"] == "TimeoutExpired"


class MalformedOnceModel(ReplayModel):
    def query(self, messages: list[dict], **kwargs) -> dict:
        if not any(m.get("content") == "malformed" for m in messages):
            raise FormatError({"role": "assistant", "content": "malformed", "extra": {"cost": 0.125}})
        return reply(["echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"])


def test_format_errors_keep_cost_and_allow_recovery(tmp_path):
    with RunStore(tmp_path) as store:
        runner = agent(store, MalformedOnceModel(outputs=[]))
        assert runner.run("test")["exit_status"] == "Submitted"
        assert runner.cost == 0.25 and runner.n_calls == 2
        assert store.load()["phase"] == "finished"


def test_portable_trajectory_excludes_provider_configuration(tmp_path):
    with RunStore(tmp_path) as store:
        runner = agent(
            store, model("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"), output_path=tmp_path / "trajectory.json"
        )
        runner.run("test")
        data = json.loads((tmp_path / "trajectory.json").read_text())
        assert "model" not in data["info"]["config"] and "environment" not in data["info"]["config"]
        assert data["trajectory_format"] == "mini-swe-agent-1.1"
        assert not (tmp_path / "trajectory.json.tmp").exists()


def test_budget_is_not_reset_after_process_restart(tmp_path):
    assert child(tmp_path, "after_reply").returncode == 77
    with RunStore(tmp_path / "run") as store:
        assert store.load()["cost"] == 0.125 and store.load()["n_calls"] == 1
    assert child(tmp_path).returncode == 0
    with RunStore(tmp_path / "run") as store:
        assert store.load()["cost"] == 0.25 and store.load()["n_calls"] == 2


def test_expired_wall_time_after_restart_blocks_new_model_query(tmp_path):
    with RunStore(tmp_path) as store:
        runner = agent(store, model("echo should-not-run >> count"), wall_time_limit_seconds=1)
        runner.extra_template_vars = {"task": "test"}
        runner._start_time = 0
        runner._checkpoint()
        assert (
            agent(store, model("echo should-not-run >> count"), wall_time_limit_seconds=1).run("test")["exit_status"]
            == "TimeExceeded"
        )
        assert not (tmp_path / "count").exists() and store.load()["n_calls"] == 0


def test_terminal_resume_rebuilds_missing_export_without_running_actions(tmp_path):
    with RunStore(tmp_path) as store:
        provider = model("echo x >> count", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")
        path = tmp_path / "trajectory.json"
        agent(store, provider, output_path=path).run("test")
        path.unlink()
        agent(store, model("echo x >> count", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"), output_path=path).run(
            "test"
        )
        assert json.loads(path.read_text())["info"]["exit_status"] == "Submitted"
        assert (tmp_path / "count").read_text() == "x\n"
