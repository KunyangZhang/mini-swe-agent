"""Checkpointed mini-swe-agent with conservative recovery of external effects."""

import hashlib
import json
from pathlib import Path

from minisweagent import Environment, Model
from minisweagent.agents.default import DefaultAgent
from minisweagent.harness.store import RunStore, fingerprint


class DurableAgent(DefaultAgent):
    """Use with an open RunStore; saved model replies and completed actions are reused."""

    def __init__(self, model: Model, env: Environment, *, store: RunStore, observation_chars: int = 8000, **kwargs):
        super().__init__(model, env, **kwargs)
        if observation_chars < 256:
            raise ValueError("observation_chars must be at least 256")
        self.store = store
        self.observation_chars = observation_chars
        self.phase = "ready"
        self.pending: dict | None = None
        self.binding = fingerprint(
            {
                "model": model.serialize(),
                "environment": env.serialize(),
                "agent": self.config.model_dump(mode="json"),
                "observation_chars": observation_chars,
            }
        )

    def _checkpoint(self) -> None:
        self.store.checkpoint(
            {
                "binding": self.binding,
                "messages": self.messages,
                "cost": self.cost,
                "n_calls": self.n_calls,
                "n_consecutive_format_errors": self.n_consecutive_format_errors,
                "extra_template_vars": self.extra_template_vars,
                "start_time": self._start_time,
                "phase": self.phase,
                "pending": self.pending,
            }
        )

    def run(self, task: str = "", **kwargs) -> dict:
        state = self.store.load()
        if state is None:
            return super().run(task, **kwargs)
        if state["binding"] != self.binding or state["extra_template_vars"] != {"task": task, **kwargs}:
            raise ValueError("Run configuration or task changed; use the original settings or a new run directory")
        if state["phase"] == "querying":
            raise RuntimeError("Model request outcome is unknown; inspect provider billing and start a new run")
        self.messages = state["messages"]
        self.cost, self.n_calls = state["cost"], state["n_calls"]
        self.n_consecutive_format_errors = state["n_consecutive_format_errors"]
        self.extra_template_vars = state["extra_template_vars"]
        self._start_time = state["start_time"]
        self.phase, self.pending = state["phase"], state["pending"]
        if self.phase == "finished":
            self.save(self.config.output_path)
            return self.messages[-1].get("extra", {})
        return self._run_loop()

    def query(self) -> dict:
        self.phase = "querying"
        self._checkpoint()
        message = super().query()
        self.pending, self.phase = message, "executing"
        self._checkpoint()
        return message

    def step(self) -> list[dict]:
        if self.phase == "executing":
            if self.pending is None:
                raise ValueError("Missing saved model reply")
            return self.execute_actions(self.pending)
        return super().step()

    def _observation(self, step: int, ordinal: int, result: dict) -> dict:
        result = {"exception_info": ""} | result
        output = result.get("output", "")
        if len(output) <= self.observation_chars:
            return result
        digest = hashlib.sha256(output.encode("utf-8")).hexdigest()
        artifact = self.store.directory / "artifacts" / f"{step}-{ordinal}-{digest}.txt"
        artifact.parent.mkdir(exist_ok=True)
        artifact.write_text(output, encoding="utf-8")
        half = self.observation_chars // 2
        return result | {
            "output": (
                output[:half] + f"\n[Output truncated; full artifact: {artifact}; sha256: {digest}]\n" + output[-half:]
            )
        }

    def execute_actions(self, message: dict) -> list[dict]:
        outputs = []
        for ordinal, action in enumerate(message.get("extra", {}).get("actions", [])):
            result = self.store.begin(self.n_calls, ordinal, action)
            if result is None:
                result = self.env.execute(action)
                self.store.complete(self.n_calls, ordinal, result)
            outputs.append(self._observation(self.n_calls, ordinal, result))
        messages = self.add_messages(
            *self.model.format_observation_messages(message, outputs, self.get_template_vars())
        )
        self.phase, self.pending = "ready", None
        self._checkpoint()
        return messages

    def handle_uncaught_exception(self, e: Exception) -> list[dict]:
        # Preserve the pre-error recovery point instead of persisting a misleading terminal exit.
        with self.store.db:
            self.store.event("run_interrupted", {"type": type(e).__name__})
        return []

    def save(self, path: Path | None, *extra_dicts) -> dict:
        if self.messages and self.messages[-1].get("role") == "exit":
            self.phase, self.pending = "finished", None
        elif self.phase == "querying" and self.n_consecutive_format_errors > (
            (self.store.load() or {}).get("n_consecutive_format_errors", 0)
        ):
            # DefaultAgent already recorded the recoverable FormatError and its billed cost.
            self.phase = "ready"
        self._checkpoint()
        data = self.serialize(*extra_dicts)
        # Avoid writing provider credentials from model/env configuration into portable trajectories.
        data["info"]["config"] = {"agent": self.config.model_dump(mode="json"), "binding": self.binding}
        data["info"]["harness"] = {"phase": self.phase, "journal_version": 1}
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
        return data
