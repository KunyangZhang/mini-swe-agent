import pytest

from minisweagent.agents.durable import DurableAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.harness.store import RunStore
from minisweagent.models.test_models import (
    DeterministicResponseAPIToolcallModel,
    DeterministicToolcallModel,
    make_response_api_output,
    make_toolcall_output,
)


@pytest.mark.parametrize(("response_api",), [(False,), (True,)])  # noqa: PT006 - repository requires tuple names
def test_durable_preserves_tool_call_response_pairing(tmp_path, response_api):
    actions = [{"command": "echo protocol-ok", "tool_call_id": "call_1"}]
    provider = (
        DeterministicResponseAPIToolcallModel(outputs=[make_response_api_output("test", actions)])
        if response_api
        else DeterministicToolcallModel(
            outputs=[
                make_toolcall_output(
                    "test",
                    [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "bash", "arguments": '{"command":"echo protocol-ok"}'},
                        }
                    ],
                    actions,
                )
            ]
        )
    )
    with RunStore(tmp_path) as store:
        runner = DurableAgent(
            provider,
            LocalEnvironment(cwd=str(tmp_path)),
            store=store,
            system_template="test",
            instance_template="{{task}}",
            step_limit=1,
            cost_limit=0,
        )
        assert runner.run("test")["exit_status"] == "LimitsExceeded"
        observation = runner.messages[3]
        assert observation.get("call_id", observation.get("tool_call_id")) == "call_1"
        assert "protocol-ok" in observation.get("output", observation.get("content", ""))
