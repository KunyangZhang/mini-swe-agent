# Durable Coding Agent Harness

A reliability extension of [SWE-agent/mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent), based on commit `04d809ceab9df28f9adaed044884180159172930` (2.4.6). The upstream project supplies the agent loop, model adapters, shell environments, and trajectory format. This fork adds durable execution state and recovery experiments. It is an independent portfolio fork, not an accepted upstream contribution.

When a coding agent appends to a file and its process dies, restarting the task may append twice. A saved conversation alone cannot prove whether the shell action completed. This harness records an action intent before execution and its result afterward. Recovery reuses recorded results; an intent without a result requires a verified manual outcome.

## What is implemented

- `DurableAgent`: persists model replies, messages, call counts, known costs, format-error count, task identity, and original wall-clock start; resumes a partially completed action batch.
- `RunStore`: SQLite WAL with FULL synchronous writes, transactional snapshots and action/event records, plus a Linux advisory lock that prevents cooperating writers from sharing a run.
- Action identity is `(model call, ordinal)`, with command equality checked on resume. Identical commands in different turns still run normally.
- Unknown external outcomes stop execution. `resolve` supplies an independently verified observation and records the reason; it never executes the command again.
- Large tool outputs retain their complete journal result and a content-addressed text artifact; only the head and tail are passed back to the model. This bounds observation text, not total context or subprocess memory.
- `run`, `inspect`, and `resolve` CLI commands; portable upstream-compatible JSON trajectories with provider/environment configuration excluded.
- Network-free regression using scripted replies, actual local shell commands, concurrent processes, and abrupt `os._exit` fault injection.

## Install and reproduce

Python 3.10+ on Linux/Ubuntu WSL; the tested interpreter is Python 3.12.14.

```bash
git clone --branch feat/durable-harness https://github.com/KunyangZhang/mini-swe-agent.git
cd mini-swe-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e . pytest pytest-asyncio pytest-cov ruff pre-commit
pytest tests/harness tests/agents/test_default.py tests/environments/test_local.py -q
python -m tests.harness.evaluate --output evidence/recovery-report.json
```

A reproducible dependency snapshot for this execution is in `evidence/requirements-tested.txt`. It records the tested environment, not a promise of future compatibility.

See [the small code-fix demo](examples/durable_harness/README.md) for a complete offline command-to-test-to-submission run. The replay model executes a predetermined transcript; it does not demonstrate autonomous model reasoning.

## Use a real model

Configure the credentials required by an upstream model adapter in your environment and choose the model ID yourself:

```bash
python -m minisweagent.run.extra.durable run /tmp/my-agent-run \
  --task 'Fix the failing tests in this disposable checkout' \
  --workspace /absolute/path/to/disposable-checkout \
  --model "$HARNESS_MODEL" --steps 20 --cost 1 --wall-time 600
```

Repeat exactly the same command to resume. Model, environment, agent settings, and task are fingerprinted; changing them requires a new run directory. Model serialization should be stable across constructions. The CLI uses the upstream local environment: **this is not a sandbox**, and commands have the invoking user's privileges. Use a disposable workspace; plug in an upstream Docker environment through the Python interface when isolation is required. Container persistence/restoration is not implemented or tested by this fork.

No paid model evaluation was performed for the checked-in report. The model adapter integration is available, but live-provider behavior is not claimed as validated.

## Repeated crash campaign (2026-09-26)

```bash
python -m tests.harness.benchmark --repeats 25 --workers 4
```

[Recorded results](evidence/repeated-recovery.json): seven fixed fault boundaries, each repeated 25 times, for **175 durable runs**. **100 automatically completed**, **75 blocked on an unknown action/model outcome**, and **zero duplicated counter writes** were observed. The comparison uses 25 fresh restarts of the unmodified `DefaultAgent` after the side effect; all 25 duplicated that write. These are observed fault-campaign counts, not production availability or universal exactly-once guarantees. Repetitions exercise the same seven boundaries. Blocked runs are not counted as completed tasks.

Each case starts a real process, forces abrupt exit, starts another process, and checks the resulting file. The benchmark fails if an automatically completed durable run does not produce exactly one write. Full per-case outcomes are saved so counts can be audited.

## Recovery contract

```text
ready -> querying -> executing -> ready
                         |
             intent -> shell -> saved result
                         |
               unknown outcome: stop

exit message -> finished (subsequent invocation returns the saved result)
```

| Durable state at process death | Next invocation |
| --- | --- |
| Model reply persisted, action not started | Execute the saved action, no model re-query |
| Action result persisted, observation not persisted | Reconstruct observation from recorded result |
| Observation persisted | Continue with saved messages and accounting |
| Action intent exists but result missing | Stop; inspect side effects and resolve manually |
| Model request in flight | Stop; provider billing and response are unknown; use a new run after inspection |
| Terminal snapshot persisted | Return saved terminal result, execute nothing |

```bash
python -m minisweagent.run.extra.durable inspect /tmp/my-agent-run
# Create /tmp/verified-result.json from your actual inspection:
# {"output": "the verified output", "returncode": 0}
python -m minisweagent.run.extra.durable resolve /tmp/my-agent-run 1 0 \
  --result /tmp/verified-result.json --reason 'Describe how the outcome was independently checked'
```

For a command confirmed not to have run, an operator may explicitly accept a skipped outcome using a nonzero return code and explanatory output, allowing the model to decide what to do next. Resolution is a human assertion, not proof supplied by the harness. It must not be filled with a guessed success result.

## Evidence and limitations

Local validation on 2026-09-25: **98 tests passed**, including **30 extension tests** and **68 related upstream tests**. See [test output](evidence/tests.txt) and [JUnit XML](evidence/tests.xml). Main-process coverage for `DurableAgent` and the harness package is 95%; child-process-only paths and CLI coverage are not included, so this is not whole-project coverage.

The [checked-in report](evidence/recovery-report.json) contains seven distinct process-death scenarios: four recover automatically, two stop for unknown command outcomes and finish after resolution, and one stops for an unknown model request. Counter files confirm no duplicate automatic writes in these scenarios. The comparison is deliberately narrow: the unmodified `DefaultAgent` started as a fresh run after an after-effect crash writes twice. It is not a comparison against every possible upstream recovery policy.

- Arbitrary shell side effects are **not exactly-once**. SQLite cannot transact with a shell, remote service, or file system. The design prefers stopping over unverified retry.
- A child command can outlive a killed runner. Before resolving an unknown action, confirm the old process has stopped and inspect the workspace. This fork does not supervise detached commands across crashes.
- Budgets follow upstream semantics: calls and known cost persist; cost can overshoot by one response. Wall time includes downtime but is checked between model requests, not a hard real-time deadline for every pending command.
- The full workspace, provider state, credentials, and OS environment are not snapshotted. The binding checks configured values, not arbitrary external state. Resume requires the same persistent workspace.
- A submit command uses the upstream `Submitted` control-flow exception. If the terminal checkpoint exists, it is never rerun. If death occurs before that checkpoint, the intent is ambiguous. The journal may show an uncompleted final action even for a finished run because completion is represented by the terminal snapshot.
- Journal and artifacts can contain prompts, code, shell output, and user-supplied secrets. Removing provider configuration from JSON exports is not general redaction.
- Locks assume one local machine/file system. No distributed workers or network-file-system guarantees.
- No SWE-bench score, model pass-rate improvement, production uptime, or user adoption is claimed.

## Code map and attribution

| File | Responsibility |
| --- | --- |
| `src/minisweagent/agents/default.py` | Extracts the unchanged upstream loop into `_run_loop` for reuse |
| `src/minisweagent/agents/durable.py` | State restoration, reply/action recovery, bounded observations, trajectory export |
| `src/minisweagent/harness/store.py` | Persistent journal, writer exclusion, explicit resolution |
| `src/minisweagent/harness/replay.py` | Stateless scripted provider for repeatable tests |
| `src/minisweagent/run/extra/durable.py` | CLI composition |
| `tests/harness/` | Behavioral tests, real process fault injection, reproducible experiment |

All upstream attribution and the MIT license are retained. The extension was developed with Codex assistance. The project owner should read the code, reproduce the experiments, and understand the recovery boundary before presenting it as interview experience.
