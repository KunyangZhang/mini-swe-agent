# Offline coding-task demonstration

This demonstration uses a scripted model to isolate harness behavior. It executes actual shell commands, changes a Python function, runs real pytest checks, and stores a submission. It is not a live LLM evaluation.

From the repository root, with the virtual environment activated:

```bash
source .venv/bin/activate
DEMO_WORKSPACE=$(mktemp -d /tmp/durable-demo-XXXXXX)
cp examples/durable_harness/fixture/stats.py.txt "$DEMO_WORKSPACE/stats.py"
cp examples/durable_harness/fixture/test_stats.py.txt "$DEMO_WORKSPACE/test_stats.py"
python -m minisweagent.run.extra.durable run "$DEMO_WORKSPACE/session" \
  --task 'Fix mean for fractional and negative inputs' \
  --workspace "$DEMO_WORKSPACE" --replay examples/durable_harness/replay.json
python -m minisweagent.run.extra.durable inspect "$DEMO_WORKSPACE/session"
```

The transcript first observes two failing assertions, replaces floor division with true division, observes all three fixture tests passing, and submits. Repeat the `run` command with the same directory and arguments: it returns the saved submission without rerunning commands. The portable trajectory is `$DEMO_WORKSPACE/session/trajectory.json`.

The task fixtures use `.py.txt` names so the intentionally failing starting version is not collected by the repository's test suite. Do not interpret the fixture as a newly discovered bug in mini-swe-agent.
