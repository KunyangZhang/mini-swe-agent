"""Network-free scripted model for harness regression, not model-quality evaluation."""

from minisweagent.models.test_models import DeterministicModel


class ReplayModel(DeterministicModel):
    def query(self, messages: list[dict], **kwargs) -> dict:
        self.current_index = sum(message.get("role") == "assistant" for message in messages) - 1
        return super().query(messages, **kwargs)
