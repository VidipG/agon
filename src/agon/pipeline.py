"""
Pipeline orchestrator.

Mode routing
------------
  eigentest  — mechanical invariant extraction only (no mutations)
  mutagen    — eigentest + mutation generation + sandbox execution
  analyze    — alias for mutagen (full Phase 1 pipeline)
  diff       — same as analyze, scoped to a VCS diff (scope handled upstream)
  bootstrap  — same as eigentest; used on first run to build an invariant baseline

Phase 0: eigentest mechanical extraction only.
Phase 1: mutagen integrated.
Phase 2: LLM chain added to eigentest.
Phase 3: spectre integrated.
"""
from __future__ import annotations

from pathlib import Path

from .adapters.base import LanguageAdapter
from .config import AgonConfig, load_config
from .eigentest.engine import EigentestEngine, EigentestResult
from .models.schema import AgonReport, Mutation, MutationStatus, ReportSummary
from .triggers.base import RunRequest


def run(request: RunRequest) -> AgonReport:
    """Execute the pipeline for the given RunRequest and return a report."""
    cfg = load_config(request.config_path)
    adapter = _resolve_adapter(cfg)
    project_root = detect_project_root(request.scope.paths)

    if request.mode in ("eigentest", "bootstrap"):
        eigen = _run_eigentest(request, cfg, adapter, project_root)
        return _build_report(request, eigen, mutations=[], project_root=project_root)

    if request.mode in ("mutagen", "analyze", "diff"):
        return _run_mutagen_pipeline(request, cfg, adapter, project_root)

    raise NotImplementedError(
        f"Mode '{request.mode}' is not yet implemented. "
        "Available: eigentest, mutagen, analyze, diff, bootstrap"
    )


def _resolve_adapter(cfg: AgonConfig) -> LanguageAdapter:
    """Instantiate the correct LanguageAdapter for the configured language."""
    language = cfg.general.language.lower()
    if language == "python":
        from .adapters.python import PythonAdapter
        return PythonAdapter()
    raise ValueError(
        f"Unsupported language: {language!r}. "
        "Supported languages: python"
    )


# ---------------------------------------------------------------------------
# Eigentest-only path
# ---------------------------------------------------------------------------


def _run_eigentest(
    request: RunRequest,
    cfg: AgonConfig,
    adapter: LanguageAdapter,
    project_root: Path,
) -> EigentestResult:
    engine = EigentestEngine(adapter=adapter)
    return engine.run(
        paths=request.scope.paths,
        functions_filter=request.scope.functions or None,
        project_root=project_root,
    )


# ---------------------------------------------------------------------------
# Full mutagen pipeline
# ---------------------------------------------------------------------------


def _run_mutagen_pipeline(
    request: RunRequest,
    cfg: AgonConfig,
    adapter: LanguageAdapter,
    project_root: Path,
) -> AgonReport:
    from .mutagen.engine import MutagenEngine
    from .sandbox.process import SandboxRunner

    # Phase 1: invariant extraction
    eigen = _run_eigentest(request, cfg, adapter, project_root)

    # Phase 2: mutation generation
    mutagen_engine = MutagenEngine(adapter=adapter)
    mutagen_result = mutagen_engine.run(
        functions=eigen.functions,
        invariants=eigen.invariants,
        config=cfg,
    )

    # Phase 3: sandbox execution
    runner = SandboxRunner(adapter=adapter, config=cfg)
    sandbox_result = runner.run(
        mutations=mutagen_result.mutations,
        functions=eigen.functions,
        project_root=project_root,
    )

    return _build_report(
        request,
        eigen,
        mutations=sandbox_result.mutations,
        project_root=project_root,
        baseline_failures=sandbox_result.baseline_failures,
    )


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------


def _build_report(
    request: RunRequest,
    eigen: EigentestResult,
    mutations: list[Mutation],
    project_root: Path,
    baseline_failures: list[str] | None = None,
) -> AgonReport:
    # Invariant source breakdown
    by_source: dict[str, int] = {}
    for inv in eigen.invariants:
        key = inv.source.value
        by_source[key] = by_source.get(key, 0) + 1

    # Mutation statistics
    killed = sum(1 for m in mutations if m.status == MutationStatus.killed)
    survived = sum(1 for m in mutations if m.status == MutationStatus.survived)
    equivalent = sum(1 for m in mutations if m.status == MutationStatus.equivalent)
    scoreable = killed + survived  # exclude equivalent, timeout, error, pending
    score = (killed / scoreable) if scoreable > 0 else 0.0

    summary = ReportSummary(
        functions_analyzed=len(eigen.functions),
        invariants_inferred=len(eigen.invariants),
        invariants_by_source=by_source,
        mutations_generated=len(mutations),
        mutations_killed=killed,
        mutations_survived=survived,
        mutations_equivalent=equivalent,
        mutation_score=score,
    )

    return AgonReport(
        project=str(project_root),
        scope=[str(p) for p in request.scope.paths],
        invariants=eigen.invariants,
        mutations=mutations,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Project-root detection (unchanged from Phase 0)
# ---------------------------------------------------------------------------

_PROJECT_ROOT_MARKERS: tuple[str, ...] = (
    ".git", ".hg", ".svn",
    "pyproject.toml", "setup.py", "setup.cfg",
    "package.json", "go.mod", "Cargo.toml", "pom.xml", "build.gradle",
    ".agon", "agon.toml",
)


def detect_project_root(paths: list[Path]) -> Path:
    """Find the project root for the given analysis paths.

    See module docstring for the algorithm.
    """
    if not paths:
        return Path.cwd()

    candidates: set[Path] = set()
    for path in paths:
        start = path.resolve()
        anchor = _walk_up_for_marker(start if start.is_dir() else start.parent)
        if anchor is not None:
            candidates.add(anchor)

    if not candidates:
        return Path.cwd()

    return min(candidates, key=lambda p: len(p.parts))


def _walk_up_for_marker(directory: Path) -> Path | None:
    current = directory.resolve()
    visited: set[Path] = set()
    while current not in visited:
        visited.add(current)
        if any((current / marker).exists() for marker in _PROJECT_ROOT_MARKERS):
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return None
