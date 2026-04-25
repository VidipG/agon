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

from .models.schema import AgonReport
from .pipeline import run as pipeline_run
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
    """Dispatch a RunRequest to the pipeline engine and render the output."""
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

    try:
        report = pipeline_run(request)
    except NotImplementedError as exc:
        err_console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(1) from exc
    except Exception as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc

    _render(report, request.output_format)


def _render(report: AgonReport, output_format: str) -> None:
    """Render the report in the requested format."""
    import json

    if output_format == "json":
        print(report.model_dump_json(indent=2))
        return

    if output_format == "sarif":
        print(_to_sarif(report))
        return

    if output_format == "markdown":
        _render_markdown(report)
        return

    # Default: terminal (Rich)
    _render_terminal(report)


def _render_terminal(report: AgonReport) -> None:
    from rich.table import Table

    s = report.summary
    console.print(Panel(
        f"[bold]Functions analyzed:[/bold] {s.functions_analyzed}\n"
        f"[bold]Invariants inferred:[/bold] {s.invariants_inferred}\n"
        f"[bold]By source:[/bold] {dict(s.invariants_by_source)}",
        title="[cyan]agon — eigentest report[/cyan]",
        border_style="cyan",
    ))

    if not report.invariants:
        console.print("[dim]No invariants found.[/dim]")
        return

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Function", style="cyan", no_wrap=True)
    table.add_column("Category", style="yellow")
    table.add_column("Confidence", justify="right")
    table.add_column("Property")

    for inv in sorted(report.invariants, key=lambda i: (-i.confidence, i.function_refs[0].name)):
        ref = inv.function_refs[0]
        table.add_row(
            ref.name,
            inv.category.value,
            f"{inv.confidence:.2f}",
            inv.property[:80],
        )

    console.print(table)


def _render_markdown(report: AgonReport) -> None:
    s = report.summary
    lines = [
        "## Agon — Eigentest Report",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Functions analyzed | {s.functions_analyzed} |",
        f"| Invariants inferred | {s.invariants_inferred} |",
        "",
        "### Invariants",
        "",
        "| Function | Category | Confidence | Property |",
        "|----------|----------|------------|----------|",
    ]
    for inv in report.invariants:
        ref = inv.function_refs[0]
        lines.append(
            f"| `{ref.name}` | {inv.category.value} | {inv.confidence:.2f} | {inv.property[:60]} |"
        )
    console.print("\n".join(lines))


def _to_sarif(report: AgonReport) -> str:
    import json

    results = []
    for inv in report.invariants:
        ref = inv.function_refs[0]
        results.append({
            "ruleId": f"agon/{inv.category.value}",
            "message": {"text": inv.property},
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": ref.file},
                    "region": {"startLine": ref.line_start},
                }
            }],
            "properties": {
                "confidence": inv.confidence,
                "source": inv.source.value,
            },
        })

    sarif = {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0",
        "runs": [{"tool": {"driver": {"name": "agon", "version": "0.1.0"}}, "results": results}],
    }
    return json.dumps(sarif, indent=2)
