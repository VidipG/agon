"""
Git-based scope resolution for diff mode.

Resolves the set of files that changed between a git ref and the current
working tree (or HEAD).  Used by the pipeline to narrow analysis to only
the code that actually changed.

Strategies
----------
- With base_ref: ``git diff --name-only <base>...HEAD`` — three-dot merge-base
  diff.  This is what CI uses: "everything on this branch that main doesn't have."
- Without base_ref: ``git diff --name-only HEAD`` plus
  ``git diff --name-only --cached HEAD`` — all uncommitted changes (staged +
  unstaged), useful for local development.

In both cases only files that exist on disk are returned (deleted files are
excluded — the engine cannot analyse code that is gone).
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def changed_files(project_root: Path, base_ref: str | None = None) -> list[Path]:
    """Return absolute paths of source files that changed vs *base_ref*.

    Args:
        project_root: Directory that contains the ``.git`` folder.
        base_ref: Git ref (branch, tag, SHA) to compare against.  When
                  ``None``, returns files with uncommitted changes.

    Returns:
        Deduplicated, sorted list of existing absolute paths.
        Empty list if git is unavailable or no changes detected.
    """
    try:
        if base_ref:
            names = _git_diff_names(project_root, [f"{base_ref}...HEAD"])
            if not names:
                # Fallback: plain two-dot diff (handles shallow clones / detached HEAD)
                names = _git_diff_names(project_root, [base_ref, "HEAD"])
        else:
            # Unstaged changes
            unstaged = _git_diff_names(project_root, ["HEAD"])
            # Staged changes
            staged = _git_diff_names(project_root, ["--cached", "HEAD"])
            names = unstaged | staged
    except FileNotFoundError:
        logger.warning("git_scope: git executable not found — diff mode unavailable")
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("git_scope: failed to resolve changed files: %s", exc)
        return []

    paths: list[Path] = []
    for name in sorted(names):
        p = (project_root / name).resolve()
        if p.exists():
            paths.append(p)

    logger.debug("git_scope: %d changed file(s) vs %s", len(paths), base_ref or "HEAD")
    return paths


def filter_to_scope(
    changed: list[Path],
    scope_paths: list[Path],
    adapter_extensions: tuple[str, ...],
) -> list[Path]:
    """Narrow *changed* to files under *scope_paths* with recognised extensions.

    Args:
        changed: Absolute paths from ``changed_files()``.
        scope_paths: Files or directories the user asked to analyse.
        adapter_extensions: Source extensions from the language adapter (e.g. ``(".py",)``).

    Returns:
        Filtered list, or the original *scope_paths* if nothing matches
        (so the caller falls back to a full analysis rather than analysing nothing).
    """
    # Resolve every scope path to a canonical form for prefix-checking
    resolved_scopes = [p.resolve() for p in scope_paths]

    filtered: list[Path] = []
    for p in changed:
        if p.suffix not in adapter_extensions:
            continue
        for scope in resolved_scopes:
            if scope.is_dir():
                try:
                    p.relative_to(scope)
                    filtered.append(p)
                    break
                except ValueError:
                    continue
            elif p == scope:
                filtered.append(p)
                break

    if not filtered:
        logger.debug("git_scope: no changed files match scope — falling back to full analysis")
        return scope_paths

    return filtered


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _git_diff_names(cwd: Path, args: list[str]) -> set[str]:
    """Run ``git diff --name-only <args>`` and return the set of file names."""
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMRT", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}
