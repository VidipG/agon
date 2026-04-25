"""
Pipeline orchestrator.

Translates a RunRequest into a pipeline execution:
  - "eigentest" mode: mechanical invariant extraction only
  - "analyze"   mode: eigentest only for now (mutagen + spectre added in later phases)
  - Other modes: stub that reports not-yet-implemented

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
from .models.schema import AgonReport, ReportSummary
from .triggers.base import RunRequest


def run(request: RunRequest) -> AgonReport:
    """Execute the pipeline for the given RunRequest and return a report."""
    cfg = load_config(request.config_path)
    adapter = _resolve_adapter(cfg)
    project_root = detect_project_root(request.scope.paths)

    if request.mode in ("eigentest", "analyze", "diff", "bootstrap"):
        result = _run_eigentest(request, cfg, adapter, project_root)
        return _build_report(request, result, project_root)

    raise NotImplementedError(
        f"Mode '{request.mode}' is not yet implemented. "
        "Available: eigentest, analyze, diff, bootstrap"
    )


def _resolve_adapter(cfg: AgonConfig) -> LanguageAdapter:
    """Instantiate the correct LanguageAdapter for the configured language.

    Currently only Python is supported. Additional adapters are registered
    here as they are implemented (TypeScript, Go, etc.).
    """
    language = cfg.general.language.lower()
    if language == "python":
        from .adapters.python import PythonAdapter
        return PythonAdapter()
    raise ValueError(
        f"Unsupported language: {language!r}. "
        "Supported languages: python"
    )


# Filesystem markers that indicate a project root, ordered by specificity.
# The first marker found while walking up wins.
_PROJECT_ROOT_MARKERS: tuple[str, ...] = (
    # Version-control roots — the strongest signal
    ".git",
    ".hg",
    ".svn",
    # Language package manifests
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "package.json",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    # Agon's own config — explicit override
    ".agon",
    "agon.toml",
)


def detect_project_root(paths: list[Path]) -> Path:
    """Find the project root for the given analysis paths.

    The project root is the directory that contains the package manifest or
    VCS root. It determines:
      - Where tests are invoked from (mutagen)
      - The base for relative FunctionRef.file paths (so SARIF annotations
        land on the right lines in GitHub Code Scanning)
      - Where .agon/config.toml and .agon/usage.db live

    Algorithm: for each analysis path, walk upward until a project root
    marker is found (.git, pyproject.toml, etc.). If multiple paths produce
    different candidate roots, use the shallowest (outermost) one — that is
    the monorepo root. Falls back to cwd if no marker is found, which is
    correct for the common case where agon is invoked from the project root.
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
        # No marker found anywhere — fall back to cwd. This is the right
        # default: CI/CD systems (GitHub Actions, etc.) set cwd to the
        # checkout root before invoking agon, so cwd IS the project root
        # even when there's no marker in the subtree being analyzed.
        return Path.cwd()

    # If multiple candidates (e.g. src/a and src/b each have their own
    # pyproject.toml), prefer the shallowest — that is the monorepo root.
    return min(candidates, key=lambda p: len(p.parts))


def _walk_up_for_marker(directory: Path) -> Path | None:
    """Walk up from directory until a project root marker is found.

    Returns the directory containing the marker, or None if the filesystem
    root is reached without finding one.
    """
    current = directory.resolve()
    # Guard against infinite loops on unusual filesystems
    visited: set[Path] = set()
    while current not in visited:
        visited.add(current)
        if any((current / marker).exists() for marker in _PROJECT_ROOT_MARKERS):
            return current
        parent = current.parent
        if parent == current:
            # Reached filesystem root
            return None
        current = parent
    return None


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


def _build_report(
    request: RunRequest,
    result: EigentestResult,
    project_root: Path,
) -> AgonReport:
    by_source: dict[str, int] = {}
    for inv in result.invariants:
        key = inv.source.value
        by_source[key] = by_source.get(key, 0) + 1

    summary = ReportSummary(
        functions_analyzed=len(result.functions),
        invariants_inferred=len(result.invariants),
        invariants_by_source=by_source,
    )

    return AgonReport(
        project=str(project_root),
        scope=[str(p) for p in request.scope.paths],
        invariants=result.invariants,
        summary=summary,
    )
