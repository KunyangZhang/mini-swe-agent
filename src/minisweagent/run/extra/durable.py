"""Run, inspect and resolve a durable coding-agent session."""

import json
from pathlib import Path

import typer
import yaml

from minisweagent import package_dir
from minisweagent.agents.durable import DurableAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.harness.replay import ReplayModel
from minisweagent.harness.store import RunStore
from minisweagent.models import get_model

app = typer.Typer(no_args_is_help=True)


@app.command()
def run(
    directory: Path,
    task: str = typer.Option(...),
    workspace: Path = typer.Option(..., exists=True, file_okay=False),
    replay: Path | None = typer.Option(None, exists=True, dir_okay=False),
    model: str | None = None,
    config: Path = package_dir / "config" / "default.yaml",
    steps: int = typer.Option(20, min=1),
    cost: float = typer.Option(1.0, min=0.001),
    timeout: int = typer.Option(30, min=1),
    wall_time: int = typer.Option(600, min=1),
    observation_chars: int = typer.Option(8000, min=256),
) -> None:
    """Start/resume with identical arguments. Local commands run with your OS permissions."""
    if (replay is None) == (model is None):
        raise typer.BadParameter("Choose exactly one of --replay or --model")
    settings = yaml.safe_load(config.read_text())
    provider = (
        ReplayModel(outputs=json.loads(replay.read_text())) if replay else get_model(model, settings.get("model", {}))
    )
    with RunStore(directory) as store:
        agent = DurableAgent(
            provider,
            LocalEnvironment(cwd=str(workspace.resolve()), timeout=timeout),
            store=store,
            observation_chars=observation_chars,
            **(
                settings["agent"]
                | {
                    "step_limit": steps,
                    "cost_limit": cost,
                    "wall_time_limit_seconds": wall_time,
                    "output_path": store.directory / "trajectory.json",
                }
            ),
        )
        result = agent.run(task)
        typer.echo(json.dumps(result, ensure_ascii=False))
        if result.get("exit_status") != "Submitted":
            raise typer.Exit(2)


@app.command()
def inspect(directory: Path) -> None:
    """Print journal state and ordered events; does not execute actions or query a model."""
    if not (directory / "journal.sqlite3").is_file():
        raise typer.BadParameter("No journal exists here")
    with RunStore(directory) as store:
        typer.echo(json.dumps(store.status(), indent=2, ensure_ascii=False))


@app.command()
def resolve(
    directory: Path,
    step: int,
    ordinal: int,
    result: Path = typer.Option(..., exists=True, dir_okay=False),
    reason: str = typer.Option(...),
) -> None:
    """Supply an independently verified outcome for an ambiguous action; never rerun it."""
    if not (directory / "journal.sqlite3").is_file():
        raise typer.BadParameter("No journal exists here")
    with RunStore(directory) as store:
        store.resolve(step, ordinal, json.loads(result.read_text()), reason)
        typer.echo("Outcome recorded. Resume with the original run command.")


if __name__ == "__main__":
    app()
