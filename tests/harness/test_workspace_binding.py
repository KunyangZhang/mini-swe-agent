import pytest

from tests.harness.workspace_campaign import run_case


@pytest.mark.parametrize(("mode",), [("default",), ("relative",), ("absolute",)])  # noqa: PT006
def test_resume_binds_actual_workspace_not_launcher_spelling(tmp_path, mode):
    result = run_case(tmp_path / mode, mode)
    assert result["processes"]["interruption"]["returncode"] == 77
    assert result["processes"]["changed-launcher"]["rejected_binding"] == (mode != "absolute")
    assert result["original_task_passed"]
    assert result["original_tool_executions"] == 4
    assert result["original_patch_writes"] == 1
    assert result["other_tool_executions"] == result["other_patch_writes"] == 0
