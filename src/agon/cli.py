"""
Agon CLI entry point.

All commands translate their arguments into a RunRequest via CLITrigger
and pass it to the pipeline engine. The CLI never calls pipeline internals
directly — it always goes through the trigger/RunRequest boundary.

Commands:
  agon analyze <path>          Full pipeline (eigentest → mutagen → spectre)
  agon eigentest <path>        Invariant inference only
  agon mutagen <path>          Mutation testing only
  agon spectre <path>          Counterexample generation only
  agon diff [<path>]           Analyze only functions changed since last commit
  agon bootstrap <path>        Generate initial test file for unannotated code
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.panel import Panel

from .triggers.cli_trigger import CLITrigger

app = typer.Typer(
    name="agon",
    help="Invariant-guided mutation testing and counterexample generation.",
    add_completion=False,
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)

# ---------------------------------------------------------------------------
# Shared option types
# ---------------------------------------------------------------------------

PathArg = Annotated[
    list[Path],
    typer.Argument(help="Files or directories to analyze.", show_default=False),
]

SpecOption = Annotated[
    Optional[list[str]],
    typer.Option(
        "--spec", "-s",
        help=(
            "Specification source. Can be a file path, directory, OpenAPI YAML/JSON, "
            "Jira ticket ID (PROJ-123), Linear ticket ID, or URL. "
            "Repeat to add multiple sources."
        ),
    ),
]

OutputOption = Annotated[
    str,
    typer.Option("--output", "-o", help="Output format: terminal, json, sarif, markdown."),
]

ConfigOption = Annotated[
    Optional[Path],
    typer.Option("--config", "-c", help="Path to .agon/config.toml.", show_default=False),
]

DryRunOption = Annotated[
    bool,
    typer.Option("--dry-run", help="Show what would be analyzed without executing."),
]

IterateOption = Annotated[
    bool,
    typer.Option("--iterate", help="Enable iterative feedback loop (multiple passes)."),
]

FunctionsOption = Annotated[
    Optional[list[str]],
    typer.Option("--function", "-f", help="Specific function names to analyze. Repeat for multiple."),
]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def analyze(
    paths: PathArg,
    spec: SpecOption = None,
    output: OutputOption = "terminal",
    config: ConfigOption = None,
    iterate: IterateOption = False,
    dry_run: DryRunOption = False,
    functions: FunctionsOption = None,
) -> None:
    """Run the full pipeline: eigentest → mutagen → spectre."""
    request = CLITrigger(
        paths=paths,
        mode="analyze",
        specs=spec,
        config=config,
        output=output,
        iterate=iterate,
        dry_run=dry_run,
        functions=functions,
    ).parse()
    _dispatch(request)


@app.command()
def eigentest(
    paths: PathArg,
    spec: SpecOption = None,
    output: OutputOption = "terminal",
    config: ConfigOption = None,
    functions: FunctionsOption = None,
) -> None:
    """Infer behavioral invariants from code and specs."""
    request = CLITrigger(
        paths=paths,
        mode="eigentest",
        specs=spec,
        config=config,
        output=output,
        functions=functions,
    ).parse()
    _dispatch(request)


@app.command()
def mutagen(
    paths: PathArg,
    spec: SpecOption = None,
    output: OutputOption = "terminal",
    config: ConfigOption = None,
    functions: FunctionsOption = None,
) -> None:
    """Run mutation testing (uses invariants from eigentest or built-in operators)."""
    request = CLITrigger(
        paths=paths,
        mode="mutagen",
        specs=spec,
        config=config,
        output=output,
        functions=functions,
    ).parse()
    _dispatch(request)


@app.command()
def spectre(
    paths: PathArg,
    spec: SpecOption = None,
    output: OutputOption = "terminal",
    config: ConfigOption = None,
    functions: FunctionsOption = None,
) -> None:
    """Generate counterexamples for surviving mutations."""
    request = CLITrigger(
        paths=paths,
        mode="spectre",
        specs=spec,
        config=config,
        output=output,
        functions=functions,
    ).parse()
    _dispatch(request)


@app.command()
def diff(
    paths: Annotated[
        Optional[list[Path]],
        typer.Argument(help="Files or directories to scope. Defaults to the whole project."),
    ] = None,
    base: Annotated[
        Optional[str],
        typer.Option("--base", "-b", help="Base git ref to diff against (branch, SHA, or tag)."),
    ] = None,
    spec: SpecOption = None,
    output: OutputOption = "terminal",
    config: ConfigOption = None,
    iterate: IterateOption = False,
) -> None:
    """Analyze only functions changed since the last commit (or vs --base)."""
    request = CLITrigger(
        paths=paths or [Path(".")],
        mode="diff",
        specs=spec,
        git_base=base,
        config=config,
        output=output,
        iterate=iterate,
    ).parse()
    _dispatch(request)


@app.command()
def bootstrap(
    paths: PathArg,
    spec: SpecOption = None,
    output: OutputOption = "terminal",
    config: ConfigOption = None,
) -> None:
    """Generate an initial test file for unannotated code (cold-start helper)."""
    request = CLITrigger(
        paths=paths,
        mode="bootstrap",
        specs=spec,
        config=config,
        output=output,
    ).parse()
    _dispatch(request)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _dispatch(request) -> None:  # noqa: ANN001
    """Hand off a RunRequest to the pipeline engine.

    This stub will be replaced by the real pipeline invocation in Phase 1.
    For now it prints the parsed request so the CLI wiring can be verified.
    """
    if request.dry_run:
        console.print(Panel(
            f"[bold]Dry run — would execute:[/bold]\n"
            f"  mode:   {request.mode}\n"
            f"  paths:  {[str(p) for p in request.scope.paths]}\n"
            f"  specs:  {len(request.specs)} source(s)\n"
            f"  output: {request.output_format}",
            title="[cyan]agon[/cyan]",
            border_style="cyan",
        ))
        return

    # TODO (Phase 1): replace with pipeline engine invocation
    #   from .pipeline import Pipeline
    #   from .config import load_config
    #   cfg = load_config(request.config_path)
    #   report = asyncio.run(Pipeline(cfg).run(request))
    #   Renderer(request.output_format).render(report)

    err_console.print(
        "[yellow]Pipeline engine not yet implemented.[/yellow] "
        "RunRequest parsed successfully:",
        request.model_dump_json(indent=2),
    )
