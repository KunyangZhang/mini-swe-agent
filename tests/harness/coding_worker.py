"""Scripted coding work with real shell effects and process-death fault injection."""

import json
import os
import shlex
import sys
import time
from pathlib import Path

from minisweagent.agents.default import DefaultAgent
from minisweagent.agents.durable import DurableAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.harness.replay import ReplayModel
from minisweagent.harness.store import RunStore
from tests.harness.test_durable import reply

SOLUTION = """from itertools import islice


def chunked(values, size):
    if size <= 0:
        raise ValueError("size must be positive")
    iterator = iter(values)
    chunks = []
    while chunk := list(islice(iterator, size)):
        chunks.append(chunk)
    return chunks
"""
TASK = "Fix chunked: support iterables, preserve the final short chunk, return lists, reject nonpositive sizes."
FIXTURE = Path(__file__).resolve().parents[2] / "examples/durable_harness/chunking"


def append(path: Path, value: dict) -> None:
    with path.open("a") as stream:
        stream.write(json.dumps(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def tape() -> list[dict]:
    python = shlex.quote(sys.executable)
    patch = f"{python} - <<'PY'\nfrom pathlib import Path\nPath('chunking.py').write_text({SOLUTION!r})\nwith Path('patch-ledger.txt').open('a') as f: f.write('patch\\n')\nPY"
    return [
        reply(
            [
                f"cat chunking.py; {python} -m pytest -q test_chunking.py",
                patch,
                f"{python} -m pytest -q test_chunking.py",
            ],
            cost=0,
        ),
        reply(["printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\nchunked repaired\\n'"], cost=0),
    ]


class CodingStore(RunStore):
    def checkpoint(self, state: dict, kind: str = "checkpoint") -> None:
        fault = os.getenv("CODING_FAULT", "")
        if fault == "before_observation_checkpoint" and state["phase"] == "ready" and state["n_calls"] == 1:
            os._exit(77)
        super().checkpoint(state, kind)
        if fault == "after_reply" and state["phase"] == "executing":
            os._exit(77)

    def complete(self, step: int, ordinal: int, result: dict) -> None:
        super().complete(step, ordinal, result)
        if os.getenv("CODING_FAULT") == "after_patch_result" and (step, ordinal) == (1, 1):
            os._exit(77)


class CodingEnvironment(LocalEnvironment):
    def execute(self, action: dict, cwd: str = "", **kwargs) -> dict:
        if os.getenv("CODING_GATE") == "1":
            workspace = Path(self.config.cwd)
            (workspace / "gate-ready").touch()
            deadline = time.monotonic() + 30
            while not (workspace / "gate-release").exists():
                if time.monotonic() > deadline:
                    raise TimeoutError("Test gate was not released")
                time.sleep(0.02)
        append(Path(self.config.cwd) / "tool-executions.jsonl", {"command": action["command"], "pid": os.getpid()})
        result = super().execute(action, cwd, **kwargs)
        if os.getenv("CODING_FAULT") == "after_patch_effect" and "patch-ledger" in action["command"]:
            os._exit(77)
        return result


class CodingModel(ReplayModel):
    def query(self, messages: list[dict], **kwargs) -> dict:
        append(Path(os.environ["CODING_WORKSPACE"]) / "model-calls.jsonl", {"pid": os.getpid()})
        return super().query(messages, **kwargs)


def main(workspace: Path) -> None:
    os.environ["CODING_WORKSPACE"] = str(workspace)
    with CodingStore(workspace / "run") as store:
        baseline = os.getenv("CODING_BASELINE") == "1"
        agent = (DefaultAgent if baseline else DurableAgent)(
            CodingModel(outputs=tape(), cost_per_call=0),
            CodingEnvironment(cwd=str(workspace)),
            **({} if baseline else {"store": store}),
            system_template="Scripted coding mechanism test",
            instance_template="{{task}}",
            cost_limit=0,
            step_limit=4,
            output_path=workspace / "trajectory.json",
        )
        print(json.dumps(agent.run(TASK)))


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
