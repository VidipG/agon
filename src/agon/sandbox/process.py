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

Parallelism
-----------
Set ``MutagenConfig.parallel_workers > 1`` (or ``[mutagen] parallel_workers``
in .agon/config.toml) to run multiple mutants concurrently.  Each mutant
executes in its own temporary directory so there is no shared filesystem state.

The parallel implementation uses a thread pool rather than a process pool
because the work is I/O- and subprocess-bound: the GIL is released during
``subprocess.run``, so threads scale as well as processes without the overhead
of inter-process serialisation.

Execution proceeds in two phases:
  1. **Baselines** — one per function group, run in parallel.  A function whose
     baseline fails immediately marks all its mutants ``error``; those mutants
     are never submitted to phase 2.
  2. **Mutants** — all mutants for functions that passed their baseline are
     submitted concurrently.

Test selection
--------------
  1. Scan the project for test files (test_*.py / *_test.py).
  2. Grep each file for the function's leaf name.
  3. Run only the matching subset; fall back to the full test suite if nothing
     matches.
  4. Cache the selection per function name for the lifetime of the runner.
     The cache is populated serially *before* any parallel work begins, so no
     locking is required.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
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

    Set ``config.mutagen.parallel_workers > 1`` to enable concurrent execution.
    Each mutant runs in an independent temporary directory, so no locking is
    needed between workers.
    """

    def __init__(self, adapter: LanguageAdapter, config: AgonConfig) -> None:
        self._adapter = adapter
        self._config = config
        # Cache: func_name → list of test file paths (relative to project_root).
        # Populated serially in run() before any parallel work starts.
        self._test_selection_cache: dict[str, list[str] | None] = {}
        # Cache: frozenset(test_files) → bool (baseline passed).
        # Prevents re-running an identical test suite for multiple functions
        # that map to the same test file set.
        self._baseline_cache: dict[frozenset[str], bool] = {}

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(
        self,
        mutations: list[Mutation],
        functions: list[FunctionNode],
        project_root: Path,
    ) -> SandboxResult:
        """Execute all *mutations* and return a SandboxResult.

        Args:
            mutations:    Pending mutations from MutagenEngine.
            functions:    All functions analysed (used for test selection and
                          source lookup).
            project_root: Root directory; pytest is invoked from here.
        """
        # Build lookup maps
        func_map: dict[str, FunctionNode] = {f.ref.name: f for f in functions}

        # Group mutations by function so we run one baseline check per function
        by_func: dict[str, list[Mutation]] = {}
        for m in mutations:
            key = m.function_refs[0].name if m.function_refs else "__unknown__"
            by_func.setdefault(key, []).append(m)

        # Pre-populate test selection cache *serially* before any parallel work.
        # This avoids races on the cache dict and keeps I/O off the hot path.
        for func_name, _ in by_func.items():
            func = func_map.get(func_name)
            if func is not None:
                self._select_tests(func, project_root)

        workers = self._config.mutagen.parallel_workers
        cpu_cap = (os.cpu_count() or 1) * 2
        workers = min(workers, cpu_cap)
        timeout = self._timeout()

        if workers > 1:
            return self._run_parallel(by_func, func_map, project_root, timeout, workers)
        return self._run_serial(by_func, func_map, project_root, timeout)

    # ------------------------------------------------------------------
    # Serial execution
    # ------------------------------------------------------------------

    def _run_serial(
        self,
        by_func: dict[str, list[Mutation]],
        func_map: dict[str, FunctionNode],
        project_root: Path,
        timeout: float,
    ) -> SandboxResult:
        result = SandboxResult()

        for func_name, func_mutations in by_func.items():
            func = func_map.get(func_name)
            if func is None:
                for m in func_mutations:
                    result.mutations.append(m.model_copy(update={"status": MutationStatus.error}))
                continue

            test_files = self._select_tests(func, project_root)

            if not self._check_baseline(project_root, test_files, timeout, func_name):
                result.baseline_failures.append(func_name)
                for m in func_mutations:
                    result.mutations.append(m.model_copy(update={"status": MutationStatus.error}))
                continue

            source_file = project_root / func.ref.file
            for m in func_mutations:
                executed = self._run_one(m, func, source_file, project_root, test_files, timeout)
                result.mutations.append(executed)

        return result

    # ------------------------------------------------------------------
    # Parallel execution
    # ------------------------------------------------------------------

    def _run_parallel(
        self,
        by_func: dict[str, list[Mutation]],
        func_map: dict[str, FunctionNode],
        project_root: Path,
        timeout: float,
        workers: int,
    ) -> SandboxResult:
        result = SandboxResult()

        # ---- Phase 1: baselines in parallel (one per function) ----
        baseline_ok: dict[str, bool] = {}

        with ThreadPoolExecutor(max_workers=workers) as pool:
            baseline_futures: dict[Future[bool], str] = {}

            for func_name in by_func:
                func = func_map.get(func_name)
                if func is None:
                    baseline_ok[func_name] = False
                    continue
                test_files = self._select_tests(func, project_root)  # cache hit
                fut = pool.submit(
                    self._check_baseline, project_root, test_files, timeout, func_name
                )
                baseline_futures[fut] = func_name

            for fut in as_completed(baseline_futures):
                func_name = baseline_futures[fut]
                try:
                    passed = fut.result()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("sandbox: baseline worker error for %s: %s", func_name, exc)
                    passed = False
                baseline_ok[func_name] = passed
                if not passed:
                    result.baseline_failures.append(func_name)

        # ---- Phase 2: mutants in parallel ----
        with ThreadPoolExecutor(max_workers=workers) as pool:
            mutant_futures: dict[Future[Mutation], Mutation] = {}

            for func_name, func_mutations in by_func.items():
                func = func_map.get(func_name)

                if func is None or not baseline_ok.get(func_name, False):
                    for m in func_mutations:
                        result.mutations.append(
                            m.model_copy(update={"status": MutationStatus.error})
                        )
                    continue

                source_file = project_root / func.ref.file
                test_files = self._select_tests(func, project_root)  # cache hit

                for m in func_mutations:
                    fut = pool.submit(
                        self._run_one, m, func, source_file, project_root, test_files, timeout
                    )
                    mutant_futures[fut] = m

            for fut in as_completed(mutant_futures):
                original_m = mutant_futures[fut]
                try:
                    executed = fut.result()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "sandbox: parallel worker error for mutant %s: %s",
                        original_m.id, exc,
                    )
                    executed = original_m.model_copy(update={"status": MutationStatus.error})
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
            with _copy_sandbox(project_root, source_file, mutated_source, self._adapter) as sandbox_root:
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
    ) -> bool:
        """Run baseline (no mutation).  Return True if baseline passes.

        Results are memoised by the test file set: if two functions share the
        same test files the baseline is only executed once.  This method is
        otherwise pure (no shared-state mutation beyond the cache) and is safe
        to call from multiple threads when the cache is pre-warmed serially.
        """
        cache_key: frozenset[str] = frozenset(test_files) if test_files is not None else frozenset()
        if cache_key in self._baseline_cache:
            return self._baseline_cache[cache_key]

        baseline = self._adapter.run_tests(
            project_root,
            test_filter=test_files,
            timeout_seconds=timeout,
        )
        passed = baseline.passed
        self._baseline_cache[cache_key] = passed
        if not passed:
            logger.warning(
                "sandbox: baseline tests FAILED for %s — skipping mutations. "
                "stdout=%s stderr=%s",
                func_name,
                baseline.stdout[-500:],
                baseline.stderr[-500:],
            )
        return passed

    # ------------------------------------------------------------------
    # Test selection (cache-backed)
    # ------------------------------------------------------------------

    def _select_tests(
        self,
        func: FunctionNode,
        project_root: Path,
    ) -> list[str] | None:
        """Return test files relevant to *func*, or None for the full suite.

        Results are cached by function name.  The cache is populated serially
        in ``run()`` before parallel workers start, so no locking is needed.
        """
        cache_key = func.ref.name
        if cache_key in self._test_selection_cache:
            return self._test_selection_cache[cache_key]

        selection = select_tests(func, project_root, self._adapter)
        self._test_selection_cache[cache_key] = selection
        return selection

    def _timeout(self) -> float:
        """Compute per-mutant timeout in seconds."""
        return float(self._config.general.timeout_seconds) * self._config.mutagen.timeout_multiplier


# ---------------------------------------------------------------------------
# Public helper: test selection
# ---------------------------------------------------------------------------


def select_tests(
    func: FunctionNode, 
    project_root: Path, 
    adapter: LanguageAdapter,
) -> list[str] | None:
    """Identify test files that exercise *func*.

    Strategy (in order):
    1. Collect all test files using patterns from the adapter.
    2. Return those whose text contains the function's leaf name as a token.
    3. If nothing matches, return None (caller should use the full suite).

    This is intentionally simple — a future phase will use coverage-guided
    selection (pytest --cov) for a 10-100x speedup on large projects.
    """
    leaf_name = func.ref.name.split(".")[-1]
    patterns = adapter.test_file_patterns()

    test_files: list[Path] = []
    for pattern in patterns:
        test_files.extend(project_root.rglob(pattern))
    test_files.sort()

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


@contextmanager
def _copy_sandbox(
    project_root: Path,
    source_file: Path,
    mutated_content: str,
    adapter: LanguageAdapter,
) -> Generator[Path, None, None]:
    """Yield a temporary project root that contains *mutated_content* in place of *source_file*.

    The original *source_file* is **never modified**. The entire project tree
    is copied, using language-specific ignore patterns from the adapter.
    The mutated content is written to the copy, and the temp root is
    yielded. ``tempfile.TemporaryDirectory`` ensures cleanup.

    Args:
        project_root: Directory to copy.
        source_file:  Absolute path of the file being mutated.
        mutated_content: New text for the copy of *source_file*.
        adapter: LanguageAdapter providing ignore patterns.

    Yields:
        Path: Temporary project root.
    """
    rel = source_file.relative_to(project_root)
    ignore = shutil.ignore_patterns(*adapter.sandbox_ignore_patterns())

    with tempfile.TemporaryDirectory(prefix="agon_mut_") as tmp:
        sandbox_root = Path(tmp) / "project"
        shutil.copytree(
            project_root, sandbox_root, ignore=ignore,
            symlinks=False, ignore_dangling_symlinks=True,
        )
        (sandbox_root / rel).write_text(mutated_content, encoding="utf-8")
        yield sandbox_root
