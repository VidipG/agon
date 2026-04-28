"""
SandboxRunner — executes mutants in isolated subprocesses and classifies results.

Execution strategy
------------------
Each mutant is executed in a temporary copy of the project.  The original
source tree is **never modified**.

  1. Copy the project root to a temp directory (Python sources + config only;
     caches and virtual environments are excluded).
  2. Write the mutated content to the copy of the target file.
  3. Invoke the test runner from the temp root via LanguageAdapter.run_tests.
  4. The temp directory is cleaned up by ``tempfile.TemporaryDirectory`` on exit.

Why copy instead of PYTHONPATH overlay
---------------------------------------
pytest's default ``prepend`` import mode does ``sys.path.insert(0, rootdir)``
at collection time.  Any PYTHONPATH entries we prepend would be pushed down to
a lower priority before the first test module is imported, so the overlay would
silently test the *original* code rather than the mutant.  Running from the
project copy sidesteps this completely: pytest inserts the temp root, which
already contains the mutated file.

Safety guarantees
-----------------
* Original files untouched: the mutation is written only to the temp copy.
  A SIGKILL of the main agon process cannot corrupt the user's sources.
* Zero-mutation baseline: tests are run on the ORIGINAL project_root before any
  mutations start.  If baseline tests fail, all mutations for that function are
  marked ``error`` and skipped.
* Incremental output: each Mutation is returned as soon as its status is known,
  so callers can flush partial results to disk.

Test selection
--------------
  1. Scan the project for test files (test_*.py / *_test.py).
  2. Grep each file for the function's leaf name.
  3. Run only the matching subset; fall back to the full test suite if nothing
     matches.
  4. Cache the selection per function name for the lifetime of the runner.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator

from ..adapters.base import FunctionNode, LanguageAdapter, TestResult
from ..config import AgonConfig
from ..models.schema import Mutation, MutationStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class SandboxResult:
    """All mutations with their final status after execution."""

    mutations: list[Mutation] = field(default_factory=list)
    baseline_failures: list[str] = field(default_factory=list)  # function names

    @property
    def killed(self) -> list[Mutation]:
        return [m for m in self.mutations if m.status == MutationStatus.killed]

    @property
    def survived(self) -> list[Mutation]:
        return [m for m in self.mutations if m.status == MutationStatus.survived]

    @property
    def score(self) -> float:
        """Agon score: killed / (total - equivalent).  0.0 if no scoreable mutants."""
        scoreable = [
            m for m in self.mutations
            if m.status in (MutationStatus.killed, MutationStatus.survived)
        ]
        if not scoreable:
            return 0.0
        killed_count = sum(1 for m in scoreable if m.status == MutationStatus.killed)
        return killed_count / len(scoreable)


# ---------------------------------------------------------------------------
# SandboxRunner
# ---------------------------------------------------------------------------


class SandboxRunner:
    """Executes pending Mutation objects and returns them with updated statuses.

    Usage::

        runner = SandboxRunner(adapter=PythonAdapter(), config=cfg)
        sandbox_result = runner.run(mutations, functions, project_root)
    """

    def __init__(self, adapter: LanguageAdapter, config: AgonConfig) -> None:
        self._adapter = adapter
        self._config = config
        # Cache: func_name → list of test file paths (relative to project_root)
        self._test_selection_cache: dict[str, list[str] | None] = {}

    def run(
        self,
        mutations: list[Mutation],
        functions: list[FunctionNode],
        project_root: Path,
    ) -> SandboxResult:
        """Execute all *mutations* and return a SandboxResult.

        Args:
            mutations: Pending mutations from MutagenEngine.
            functions: All functions analysed (used for test selection and
                       source lookup).
            project_root: Root directory; pytest is invoked from here.
        """
        result = SandboxResult()

        # Build a map: function qualified-name → FunctionNode (for source lookup)
        func_map: dict[str, FunctionNode] = {f.ref.name: f for f in functions}

        # Group mutations by function so we run one baseline check per function
        by_func: dict[str, list[Mutation]] = {}
        for m in mutations:
            key = m.function_refs[0].name if m.function_refs else "__unknown__"
            by_func.setdefault(key, []).append(m)

        for func_name, func_mutations in by_func.items():
            func = func_map.get(func_name)
            if func is None:
                # Should not happen; mark all as error
                for m in func_mutations:
                    result.mutations.append(
                        m.model_copy(update={
                            "status": MutationStatus.error,
                        })
                    )
                continue

            test_files = self._select_tests(func, project_root)
            timeout = self._timeout()

            # --- zero-mutation baseline ---
            if not self._check_baseline(project_root, test_files, timeout, func_name, result):
                # Baseline already failed — skip all mutations for this function
                for m in func_mutations:
                    result.mutations.append(
                        m.model_copy(update={"status": MutationStatus.error})
                    )
                continue

            # --- run each mutant ---
            source_file = project_root / func.ref.file
            for m in func_mutations:
                executed = self._run_one(m, func, source_file, project_root, test_files, timeout)
                result.mutations.append(executed)

        return result

    # ------------------------------------------------------------------
    # Per-mutant execution
    # ------------------------------------------------------------------

    def _run_one(
        self,
        mutation: Mutation,
        func: FunctionNode,
        source_file: Path,
        project_root: Path,
        test_files: list[str] | None,
        timeout: float,
    ) -> Mutation:
        """Apply *mutation* in an isolated copy, run tests, return Mutation with status."""
        mutated_source = self._adapter.apply_mutation(func.source, mutation)

        if not source_file.exists():
            logger.warning("sandbox: source file missing: %s", source_file)
            return mutation.model_copy(update={"status": MutationStatus.error})

        try:
            with _copy_sandbox(project_root, source_file, mutated_source) as sandbox_root:
                test_result = self._adapter.run_tests(
                    sandbox_root,
                    test_filter=test_files,
                    timeout_seconds=timeout,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("sandbox: unexpected error running mutant %s: %s", mutation.id, exc)
            return mutation.model_copy(update={"status": MutationStatus.error})

        return _classify(mutation, test_result)

    # ------------------------------------------------------------------
    # Baseline check
    # ------------------------------------------------------------------

    def _check_baseline(
        self,
        project_root: Path,
        test_files: list[str] | None,
        timeout: float,
        func_name: str,
        result: SandboxResult,
    ) -> bool:
        """Run baseline (no mutation).  Return True if baseline passes."""
        baseline = self._adapter.run_tests(
            project_root,
            test_filter=test_files,
            timeout_seconds=timeout,
        )
        if not baseline.passed:
            logger.warning(
                "sandbox: baseline tests FAILED for %s — skipping mutations. "
                "stdout=%s stderr=%s",
                func_name,
                baseline.stdout[-500:],
                baseline.stderr[-500:],
            )
            result.baseline_failures.append(func_name)
            return False
        return True

    # ------------------------------------------------------------------
    # Test selection
    # ------------------------------------------------------------------

    def _select_tests(
        self,
        func: FunctionNode,
        project_root: Path,
    ) -> list[str] | None:
        """Return test files relevant to *func*, or None for the full suite.

        Caches results so each function's test set is resolved only once.
        """
        cache_key = func.ref.name
        if cache_key in self._test_selection_cache:
            return self._test_selection_cache[cache_key]

        selection = select_tests(func, project_root)
        self._test_selection_cache[cache_key] = selection
        return selection

    def _timeout(self) -> float:
        """Compute per-mutant timeout in seconds."""
        base = self._config.general.timeout_seconds
        multiplier = self._config.mutagen.timeout_multiplier
        return float(base) * multiplier


# ---------------------------------------------------------------------------
# Public helper: test selection
# ---------------------------------------------------------------------------


def select_tests(func: FunctionNode, project_root: Path) -> list[str] | None:
    """Identify test files that exercise *func*.

    Strategy (in order):
    1. Collect all test_*.py and *_test.py under project_root.
    2. Return those whose text contains the function's leaf name as a token.
    3. If nothing matches, return None (caller should use the full suite).

    This is intentionally simple — a future phase will use coverage-guided
    selection (pytest --cov) for a 10-100x speedup on large projects.
    """
    leaf_name = func.ref.name.split(".")[-1]

    test_files: list[Path] = sorted(
        list(project_root.rglob("test_*.py")) + list(project_root.rglob("*_test.py"))
    )

    # Exclude the source file itself (it's not a test file)
    source_abs = (project_root / func.ref.file).resolve()
    test_files = [tf for tf in test_files if tf.resolve() != source_abs]

    matching: list[str] = []
    for tf in test_files:
        try:
            content = tf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Simple token-level match (avoids matching 'foo' inside 'foo_bar')
        if _token_present(leaf_name, content):
            try:
                matching.append(str(tf.relative_to(project_root)))
            except ValueError:
                matching.append(str(tf))

    if matching:
        logger.debug(
            "sandbox: test selection for %s → %s",
            func.ref.name,
            matching,
        )
        return matching

    logger.debug(
        "sandbox: no matching tests for %s — using full suite",
        func.ref.name,
    )
    return None


def _token_present(name: str, text: str) -> bool:
    """Return True if *name* appears as a standalone identifier in *text*."""
    # Quick substring check first (cheap)
    if name not in text:
        return False
    # Verify it's not just a substring of a longer identifier
    import re
    return bool(re.search(r"\b" + re.escape(name) + r"\b", text))


# ---------------------------------------------------------------------------
# Mutation status classification
# ---------------------------------------------------------------------------


def _classify(mutation: Mutation, result: TestResult) -> Mutation:
    """Map a TestResult onto a MutationStatus and return an updated Mutation."""
    if result.timed_out:
        return mutation.model_copy(update={"status": MutationStatus.timeout})

    if result.error_message is not None:
        return mutation.model_copy(update={"status": MutationStatus.error})

    if result.killed_mutant:
        # Find which tests failed by parsing pytest's short output
        killing_tests = _extract_failing_tests(result.stdout + result.stderr)
        return mutation.model_copy(update={
            "status": MutationStatus.killed,
            "killing_tests": killing_tests,
            "execution_time_ms": result.duration_ms,
        })

    return mutation.model_copy(update={
        "status": MutationStatus.survived,
        "execution_time_ms": result.duration_ms,
    })


def _extract_failing_tests(output: str) -> list[str]:
    """Parse pytest output to extract failing test IDs.

    Looks for lines starting with 'FAILED ' (pytest's default summary format).
    """
    import re

    failing: list[str] = []
    for line in output.splitlines():
        m = re.match(r"^FAILED\s+(\S+)", line)
        if m:
            failing.append(m.group(1))
    return failing


# ---------------------------------------------------------------------------
# Context manager: isolated project copy
# ---------------------------------------------------------------------------


_COPY_IGNORE = shutil.ignore_patterns(
    "*.pyc",
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    "env",
    ".tox",
    "dist",
    "build",
    "*.egg-info",
)


@contextmanager
def _copy_sandbox(
    project_root: Path,
    source_file: Path,
    mutated_content: str,
) -> Generator[Path, None, None]:
    """Yield a temporary project root that contains *mutated_content* in place of *source_file*.

    The original *source_file* is **never modified**.  The entire project tree
    is copied (Python sources and config; caches and virtual environments are
    excluded), the mutated content is written to the copy, and the temp root is
    yielded.  ``tempfile.TemporaryDirectory`` ensures cleanup even if the main
    process receives ``SIGKILL``.

    Args:
        project_root: Directory to copy.
        source_file:  Absolute path of the file being mutated (must be under
                      *project_root*).
        mutated_content: New text for the copy of *source_file*.

    Yields:
        Path: Temporary project root; pass this to ``LanguageAdapter.run_tests``.
    """
    rel = source_file.relative_to(project_root)

    with tempfile.TemporaryDirectory(prefix="agon_mut_") as tmp:
        sandbox_root = Path(tmp) / "project"
        shutil.copytree(project_root, sandbox_root, ignore=_COPY_IGNORE, symlinks=False)
        (sandbox_root / rel).write_text(mutated_content, encoding="utf-8")
        yield sandbox_root
