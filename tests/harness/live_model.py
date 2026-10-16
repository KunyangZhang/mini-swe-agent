"""Small local-transformers integration smoke test; never used by offline acceptance."""

import json
import os
import platform
import time
from pathlib import Path

from minisweagent.exceptions import FormatError
from minisweagent.models.utils.actions_text import format_observation_messages, parse_regex_actions
from tests.harness.coding_worker import append

SYSTEM = """You are a coding agent working in a disposable directory. Fix the requested Python function.
Read the files and run the tests. Do not modify test_chunking.py or delete files.
Each response MUST contain exactly ONE bash command block, using ```bash and ``` delimiters.
The command runs in the workspace. You may use cat, Python and pytest. Do not ask the user questions.
After tests pass, submit by running: printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\ndone\\n'
Only that command completes the task. You have at most 12 model turns. Be concise."""


class TransformersModel:
    def __init__(self, model_path: Path, logs: Path):
        self.model_path = model_path.resolve()
        self.logs = logs
        self.model = None
        self.load_seconds = 0.0

    def query(self, messages: list[dict], **kwargs) -> dict:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        if self.model is None:
            started = time.monotonic()
            self.tokenizer = AutoTokenizer.from_pretrained(self.model_path, local_files_only=True)
            self.model = AutoModelForCausalLM.from_pretrained(
                self.model_path,
                local_files_only=True,
                dtype=torch.bfloat16,
                device_map={"": 0},
                attn_implementation="sdpa",
            )
            self.model.eval()
            self.load_seconds = time.monotonic() - started
        prompt_messages = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "exit"]
        prompt = self.tokenizer.apply_chat_template(prompt_messages, tokenize=False, add_generation_prompt=True)
        inputs = self.tokenizer(prompt, return_tensors="pt").to("cuda")
        started = time.monotonic()
        with torch.inference_mode():
            tokens = self.model.generate(
                **inputs, max_new_tokens=768, do_sample=False, pad_token_id=self.tokenizer.eos_token_id
            )
        torch.cuda.synchronize()
        response = self.tokenizer.decode(tokens[0, inputs.input_ids.shape[1] :], skip_special_tokens=True)
        usage = {
            "input_tokens": inputs.input_ids.shape[1],
            "output_tokens": tokens.shape[1] - inputs.input_ids.shape[1],
            "generation_seconds": time.monotonic() - started,
            "load_seconds": self.load_seconds,
            "peak_cuda_allocated_bytes": torch.cuda.max_memory_allocated(),
            "peak_cuda_reserved_bytes": torch.cuda.max_memory_reserved(),
            "pid": os.getpid(),
            "api_cost_usd": None,
        }
        append(self.logs / "model-requests.jsonl", {"messages": prompt_messages, "response": response, "usage": usage})
        message = {"role": "assistant", "content": response, "extra": {"cost": 0, "usage": usage}}
        try:
            actions = parse_regex_actions(
                response,
                action_regex=r"```bash\s*\n(.*?)```",
                format_error_template="Return exactly one bash code block. {{error}}",
            )
        except FormatError as error:
            raise FormatError(message, *error.messages) from error
        message["extra"]["actions"] = actions
        return message

    def format_message(self, **kwargs) -> dict:
        return kwargs

    def format_observation_messages(
        self, message: dict, outputs: list[dict], template_vars: dict | None = None
    ) -> list[dict]:
        return format_observation_messages(
            outputs, observation_template="returncode={{output.returncode}}\n{{output.output}}"
        )

    def get_template_vars(self, **kwargs) -> dict:
        return {}

    def serialize(self) -> dict:
        return {
            "info": {
                "config": {
                    "model": {
                        "path": str(self.model_path),
                        "generation": "greedy-768",
                        "python": platform.python_version(),
                    },
                    "model_type": "local_transformers",
                }
            }
        }


def main() -> None:
    import argparse

    from minisweagent.agents.default import DefaultAgent
    from minisweagent.agents.durable import DurableAgent
    from tests.harness.coding_worker import FIXTURE, TASK, CodingEnvironment, CodingStore

    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--include-source", action="store_true")
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    with CodingStore(workspace / "run") as store:
        agent = (DefaultAgent if args.baseline else DurableAgent)(
            TransformersModel(args.model_path, workspace),
            CodingEnvironment(cwd=str(workspace), timeout=30),
            **({} if args.baseline else {"store": store}),
            system_template=SYSTEM,
            instance_template="{{task}}\nFiles: chunking.py and test_chunking.py.",
            step_limit=12,
            cost_limit=0,
            wall_time_limit_seconds=600,
            output_path=workspace / "trajectory.json",
        )
        task = TASK
        if args.include_source:
            task += "\nInitial files (read-only test specification):\n" + "\n".join(
                source.stem + "\n" + source.read_text() for source in sorted(FIXTURE.glob("*.txt"))
            )
        print(json.dumps(agent.run(task)))


if __name__ == "__main__":
    main()
