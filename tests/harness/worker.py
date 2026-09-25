"""Real child-process crash injection used by the regression suite."""

import os
import sys
from pathlib import Path

from minisweagent.agents.default import DefaultAgent
from minisweagent.agents.durable import DurableAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.harness.replay import ReplayModel
from minisweagent.harness.store import RunStore


class CrashStore(RunStore):
    def checkpoint(self, state: dict, kind: str = "checkpoint") -> None:
        super().checkpoint(state, kind)
        if os.getenv("HARNESS_CRASH") == "after_reply" and state["phase"] == "executing":
            os._exit(77)
        if os.getenv("HARNESS_CRASH") == "after_observation" and state["phase"] == "ready":
            os._exit(77)

    def begin(self, step: int, ordinal: int, action: dict) -> dict | None:
        if os.getenv("HARNESS_CRASH") == "before_begin":
            os._exit(77)
        return super().begin(step, ordinal, action)

    def complete(self, step: int, ordinal: int, result: dict) -> None:
        super().complete(step, ordinal, result)
        if os.getenv("HARNESS_CRASH") == "after_result":
            os._exit(77)


class CrashEnvironment(LocalEnvironment):
    def execute(self, action: dict, cwd: str = "", **kwargs) -> dict:
        if os.getenv("HARNESS_CRASH") == "before_effect":
            os._exit(77)
        result = super().execute(action, cwd, **kwargs)
        if os.getenv("HARNESS_CRASH") == "after_effect":
            os._exit(77)
        return result


class CrashModel(ReplayModel):
    def query(self, messages: list[dict], **kwargs) -> dict:
        if os.getenv("HARNESS_CRASH") == "during_query":
            os._exit(77)
        return super().query(messages, **kwargs)


def main(directory: Path) -> None:
    outputs = [
        {
            "role": "assistant",
            "content": "increment",
            "extra": {
                "cost": 0.125,
                "actions": [{"command": "printf 'one\\n' >> counter"}],
            },
        },
        {
            "role": "assistant",
            "content": "submit",
            "extra": {
                "cost": 0.125,
                "actions": [{"command": "printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\ndone\\n'"}],
            },
        },
    ]
    with CrashStore(directory / "run") as store:
        agent_class = DefaultAgent if os.getenv("HARNESS_BASELINE") else DurableAgent
        agent = agent_class(
            CrashModel(outputs=outputs),
            CrashEnvironment(cwd=str(directory)),
            **({} if os.getenv("HARNESS_BASELINE") else {"store": store}),
            system_template="test",
            instance_template="{{task}}",
            cost_limit=2,
            output_path=directory / "trajectory.json",
        )
        print(agent.run("increment exactly once"))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
