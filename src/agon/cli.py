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

SaveOption = Annotated[
    Optional[Path],
    typer.Option("--save", help="Save the AgonReport as JSON to this path after the run."),
]

CacheOption = Annotated[
    Optional[Path],
    typer.Option("--cache", help="Path to a prior AgonReport JSON for incremental mutation runs."),
]

FailUnderOption = Annotated[
    Optional[float],
    typer.Option(
        "--fail-under",
        help="Exit 1 if mutation score is below this threshold (0.0–1.0). Useful in CI.",
        min=0.0,
        max=1.0,
        show_default=False,
    ),
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
    save: SaveOption = None,
    cache: CacheOption = None,
    fail_under: FailUnderOption = None,
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
        report_path=save,
        cache_path=cache,
        fail_under=fail_under,
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
    save: SaveOption = None,
    cache: CacheOption = None,
    fail_under: FailUnderOption = None,
) -> None:
    """Run mutation testing (uses invariants from eigentest or built-in operators)."""
    request = CLITrigger(
        paths=paths,
        mode="mutagen",
        specs=spec,
        config=config,
        output=output,
        functions=functions,
        report_path=save,
        cache_path=cache,
        fail_under=fail_under,
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
    save: SaveOption = None,
    cache: CacheOption = None,
    fail_under: FailUnderOption = None,
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
        report_path=save,
        cache_path=cache,
        fail_under=fail_under,
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
    _check_ci_thresholds(report, request)


def _check_ci_thresholds(report: AgonReport, request) -> None:  # noqa: ANN001
    """Exit non-zero when CI quality gates are breached."""
    from .config import load_config

    exit_code = 0

    # --fail-under: mutation score gate
    if request.fail_under is not None and report.mutations:
        if report.summary.mutation_score < request.fail_under:
            score_pct = f"{report.summary.mutation_score * 100:.1f}%"
            threshold_pct = f"{request.fail_under * 100:.1f}%"
            err_console.print(
                f"[red]FAIL:[/red] mutation score {score_pct} is below "
                f"--fail-under threshold {threshold_pct}"
            )
            exit_code = 1

    # ci.fail_on: counterexample severity gate
    if report.counterexamples:
        cfg = load_config(request.config_path)
        fail_severities = set(cfg.ci.fail_on)
        failing = [
            cx for cx in report.counterexamples
            if cx.severity.value in fail_severities
        ]
        if failing:
            err_console.print(
                f"[red]FAIL:[/red] {len(failing)} counterexample(s) with severity "
                f"in {sorted(fail_severities)} — add tests to kill the surviving mutations"
            )
            exit_code = 1

    if exit_code:
        raise typer.Exit(exit_code)


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
    from .models.schema import MutationStatus

    s = report.summary

    # Summary panel — show mutation stats when present
    summary_lines = [
        f"[bold]Functions analyzed:[/bold] {s.functions_analyzed}",
        f"[bold]Invariants inferred:[/bold] {s.invariants_inferred}",
        f"[bold]By source:[/bold] {dict(s.invariants_by_source)}",
    ]
    if report.mutations:
        score_pct = f"{s.mutation_score * 100:.1f}%"
        score_color = "green" if s.mutation_score >= 0.8 else "yellow" if s.mutation_score >= 0.5 else "red"
        summary_lines += [
            "",
            f"[bold]Mutations generated:[/bold] {s.mutations_generated}",
            f"[bold]Killed:[/bold] {s.mutations_killed}  "
            f"[bold]Survived:[/bold] {s.mutations_survived}  "
            f"[bold]Equivalent:[/bold] {s.mutations_equivalent}",
            f"[bold]Mutation score:[/bold] [{score_color}]{score_pct}[/{score_color}]",
        ]

    console.print(Panel(
        "\n".join(summary_lines),
        title="[cyan]agon report[/cyan]",
        border_style="cyan",
    ))

    # Invariants table
    if report.invariants:
        inv_table = Table(show_header=True, header_style="bold magenta", title="Invariants")
        inv_table.add_column("Function", style="cyan", no_wrap=True)
        inv_table.add_column("Category", style="yellow")
        inv_table.add_column("Confidence", justify="right")
        inv_table.add_column("Property")

        for inv in sorted(report.invariants, key=lambda i: (-i.confidence, i.function_refs[0].name)):
            ref = inv.function_refs[0]
            inv_table.add_row(
                ref.name,
                inv.category.value,
                f"{inv.confidence:.2f}",
                inv.property[:80],
            )
        console.print(inv_table)
    else:
        console.print("[dim]No invariants found.[/dim]")

    # Surviving mutations table
    survived = [m for m in report.mutations if m.status == MutationStatus.survived]
    if survived:
        mut_table = Table(show_header=True, header_style="bold red", title="Surviving Mutations (test gaps)")
        mut_table.add_column("Function", style="cyan", no_wrap=True)
        mut_table.add_column("File:Line", style="dim")
        mut_table.add_column("Operator", style="yellow")
        mut_table.add_column("Original", style="green")
        mut_table.add_column("Mutated", style="red")

        for m in sorted(survived, key=lambda x: (x.function_refs[0].name if x.function_refs else "", x.location.line)):
            ref = m.function_refs[0] if m.function_refs else None
            func_name = ref.name if ref else "?"
            file_line = f"{ref.file}:{m.location.line}" if ref else f"?:{m.location.line}"
            mut_table.add_row(
                func_name,
                file_line,
                m.operator.value,
                m.original_code[:30],
                m.mutated_code[:30],
            )
        console.print(mut_table)

    # Counterexamples table
    if report.counterexamples:
        cx_table = Table(show_header=True, header_style="bold yellow", title="Counterexamples (test stubs)")
        cx_table.add_column("Function", style="cyan", no_wrap=True)
        cx_table.add_column("Operator", style="yellow")
        cx_table.add_column("Severity", justify="center")
        cx_table.add_column("Reproducer hint", style="dim")

        severity_colors = {
            "critical": "bright_red",
            "high": "red",
            "medium": "yellow",
            "low": "green",
        }

        for cx in report.counterexamples:
            mut = next((m for m in report.mutations if m.id == cx.mutation_id), None)
            if mut is None:
                continue
            ref = mut.function_refs[0] if mut.function_refs else None
            func_name = ref.name if ref else "?"
            sev = cx.severity.value
            color = severity_colors.get(sev, "white")
            # Show the first non-comment, non-blank line of the reproducer as the hint
            hint_line = next(
                (ln.strip() for ln in cx.reproducer_code.splitlines()
                 if ln.strip() and not ln.strip().startswith("#")),
                ""
            )
            cx_table.add_row(
                func_name,
                mut.operator.value,
                f"[{color}]{sev}[/{color}]",
                hint_line[:60],
            )
        console.print(cx_table)


def _render_markdown(report: AgonReport) -> None:
    from .models.schema import MutationStatus

    s = report.summary
    lines = [
        "## Agon Report",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Functions analyzed | {s.functions_analyzed} |",
        f"| Invariants inferred | {s.invariants_inferred} |",
    ]

    if report.mutations:
        score_pct = f"{s.mutation_score * 100:.1f}%"
        lines += [
            f"| Mutations generated | {s.mutations_generated} |",
            f"| Killed | {s.mutations_killed} |",
            f"| Survived | {s.mutations_survived} |",
            f"| Mutation score | **{score_pct}** |",
        ]

    lines += [
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

    survived = [m for m in report.mutations if m.status == MutationStatus.survived]
    if survived:
        lines += [
            "",
            "### Surviving Mutations (test gaps)",
            "",
            "| Function | File:Line | Operator | Original | Mutated |",
            "|----------|-----------|----------|----------|---------|",
        ]
        for m in survived:
            ref = m.function_refs[0] if m.function_refs else None
            func_name = f"`{ref.name}`" if ref else "?"
            file_line = f"{ref.file}:{m.location.line}" if ref else "?"
            lines.append(
                f"| {func_name} | {file_line} | {m.operator.value} "
                f"| `{m.original_code[:30]}` | `{m.mutated_code[:30]}` |"
            )

    console.print("\n".join(lines))


def _to_sarif(report: AgonReport) -> str:
    import json
    from .models.schema import MutationStatus

    results = []

    for inv in report.invariants:
        ref = inv.function_refs[0]
        results.append({
            "ruleId": f"agon/invariant/{inv.category.value}",
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

    for m in report.mutations:
        if m.status != MutationStatus.survived:
            continue
        ref = m.function_refs[0] if m.function_refs else None
        results.append({
            "ruleId": f"agon/survived/{m.operator.value}",
            "level": "warning",
            "message": {
                "text": (
                    f"Surviving mutation in {ref.name if ref else '?'}: "
                    f"`{m.original_code}` → `{m.mutated_code}` "
                    f"was not killed by any test."
                )
            },
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": ref.file if ref else "?"},
                    "region": {
                        "startLine": m.location.line,
                        "startColumn": m.location.col_start + 1,
                        "endColumn": m.location.col_end + 1,
                    },
                }
            }],
            "properties": {
                "operator": m.operator.value,
                "mutation_id": m.id,
            },
        })

    sarif = {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0",
        "runs": [{"tool": {"driver": {"name": "agon", "version": "0.1.0"}}, "results": results}],
    }
    return json.dumps(sarif, indent=2)
