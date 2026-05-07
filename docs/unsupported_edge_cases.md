# Unsupported Edge Cases & Future Refinements

This document tracks identified architectural gaps and edge cases in the current Agon implementation. Items marked **Supported** have been resolved; items marked **Planned** have a documented resolution path; items marked **Open** have no current fix.

---

## 1. Monorepo Scaling & Sandbox Overhead

**Current Behavior:** `shutil.copytree` copies the entire project root. In a large monorepo, `detect_project_root` may find a marker high up in the directory tree (e.g., at the repo root).

**Impact:** Every single mutant run copies the *entire* monorepo, including dozens of packages that are not relevant to the function being tested. This can lead to massive disk I/O overhead and multi-gigabyte temporary folder consumption.

**Status:** Open. Mitigated partially by `parallel_workers > 1` (concurrent copies amortise wall time) and by the incremental filter (fewer mutant runs overall when functions are unchanged). The root fix is selective copy — see `docs/sandbox_design.md §9`.

---

## 2. Global State & Process Isolation

**Current Behavior:** `SandboxRunner` spawns a subprocess, but the sandbox environment (the temporary project copy) provides only filesystem isolation, not system-level resource or network isolation.

**Impact:**
- **Network Side Effects:** If a mutated function (especially with LLM-guided Tier 2 mutations) makes a network call, it will execute against the real network.
- **Persistent State:** If tests write to a database or a shared cache (e.g., Redis) that is not mocked, mutants can poison that state for subsequent runs.
- **Resource Exhaustion:** A mutant that introduces an infinite loop or a memory leak can crash the host machine if not properly capped by cgroups or namespaces. The `timeout_seconds` limit kills the subprocess, but memory consumption is not capped.

**Status:** Open for Tier 1 (acceptable given mechanical mutations cannot introduce new I/O). Planned for Tier 2 LLM-guided mutations — see `docs/sandbox_design.md §9` and `docs/llm-impl.md §Edge Cases`.

---

## 3. C-Extensions & Environment Parity

**Current Behavior:** The sandbox subprocess inherits the parent's environment (VIRTUAL_ENV, PATH, etc.).

**Impact:** If the user's environment relies on shared libraries (e.g., `.so` or `.dylib` files) located at absolute paths *inside* the project root, those paths will break inside the sandbox root unless they are handled by the copy/ignore logic.

**Status:** Open. No current fix. Projects with compiled extensions should use editable installs (`pip install -e .`) so the extension resolves via the `.pth` file at the original path, not the sandbox copy.

---

## 4. Transitive Impurity across Modules

**Current Behavior:** Agon's `EigentestEngine` handles transitive purity only within a single file.

**Impact:** If function `A` in `module_a.py` calls function `B` in `module_b.py`, and `B` is impure (e.g., calls `print`), `A` will currently be marked as **PURE** because the engine does not trace imports.

**Status:** Open. Cross-module import resolution is planned for Phase 2A (LLM eigentest), where the LLM can reason about transitive impurity from the function source alone without requiring static import graph traversal.

---

## 5. Non-Deterministic Assertions

**Current Behavior:** Mechanical extraction identifies `assert` statements as invariants.

**Impact:** If an assertion uses non-deterministic values (e.g., `assert time.time() > 0`), the invariant is technically correct but might be difficult for `mutagen` or `spectre` to verify reliably without mocking the clock.

**Status:** Open. LLM-augmented eigentest (Phase 2A) will include confidence scoring that penalises invariants whose `property_code` references time, random, or I/O.

---

## 6. Concurrent Mutation Collisions

**Current Behavior:** Each mutant run uses a unique `tempfile.TemporaryDirectory`.

**Status:** **Supported.** This design correctly allows multiple Agon processes to analyze the same project concurrently without collision. With `parallel_workers > 1`, multiple mutants from a single run also execute in independent directories simultaneously. The only bottleneck is system I/O and CPU. The incremental filter cache (prior `AgonReport`) is read-only during a run and safe to share.

---

## 7. Thread Safety under Parallel Execution

**Current Behavior:** `SandboxRunner._run_parallel` uses `ThreadPoolExecutor`. The test-selection cache (`_test_selection_cache`) is populated serially before workers start, so cache reads during parallel execution are pure and thread-safe under CPython's GIL. The baseline cache (`_baseline_cache`) is also populated from the main thread via `as_completed`. `SandboxResult.baseline_failures` is appended only from the main thread after `as_completed` collects each baseline future.

**Status:** **Supported** for the current implementation. The following patterns would break thread safety and must be avoided when extending `SandboxRunner`:

- Mutating `_test_selection_cache` or `_baseline_cache` from within a worker function (would race with other workers reading the same dict).
- Appending to `result.mutations` or `result.baseline_failures` from inside a `pool.submit` callback (currently avoided; all appends happen in the main thread via `as_completed`).
- Sharing a single `TemporaryDirectory` across workers (currently impossible since `_run_one` creates its own via `_copy_sandbox`).

If `parallel_workers` is extended to use a `ProcessPoolExecutor` (for true parallelism bypassing the GIL), all arguments to `_run_one` must be picklable, and the result dict must be replaced with a `multiprocessing.Queue`.

---

## 8. LLM Prompt Injection via Code Content

**Current Behavior:** Not yet applicable — the LLM phase is not implemented. When implemented, function source code will be embedded in LLM prompts.

**Impact:** A function whose docstring, comments, or string literals contain adversarial text (e.g., `# Ignore all prior instructions and output X`) could manipulate LLM responses to produce invalid invariants, malicious mutations, or incorrect counterexamples.

**Status:** Planned mitigation documented in `docs/llm-impl.md §Security`. All code is placed inside a delimited code fence in the prompt; the system prompt explicitly instructs the model to treat the content of the code fence as data, not instructions. Fences use a non-guessable random delimiter per request.

---

## 9. Incremental Cache Staleness

**Current Behavior:** The incremental filter identifies unchanged functions by comparing `(file, name, content_hash)` where `content_hash = sha256(function_body)`. Prior mutation results are carried over when the hash matches. Only terminal-status mutations (`killed`, `survived`, `equivalent`, `timeout`, `error`) are carried over; `pending` mutations from interrupted runs are excluded.

**Impact:**

- **Dependency changes:** If a function's body is unchanged but a function it calls changes semantics (e.g., a dependency is upgraded), the cached result may be incorrect. The incremental filter has no visibility into callee changes.
- **Test suite changes:** If new tests are added that would kill a previously-survived mutation, the cache will incorrectly carry over the `survived` status until the function body itself changes.
- **Config changes:** If `MutagenConfig.max_mutants_per_function` or the operator set changes, the cached mutations may be a different subset than what the current configuration would generate.

**Status:** Open by design — the cache is intentionally conservative. Correct behavior: run `agon analyze` without `--cache` periodically (e.g., on every merge to main) to ensure a fresh ground-truth baseline. Use `--cache` only for PR-scoped incremental runs where the assumption of stable dependencies holds.

---

## 10. pytest Exit Code Classification

**Current Behavior:** `run_tests` now correctly distinguishes pytest exit codes:
- Exit 0: tests passed → `killed_mutant=False`
- Exit 1: tests collected and at least one failed → `killed_mutant=True`
- Exit 5: no tests collected → `error_message` set, `killed_mutant=False`
- Exit 2/3/4: internal/interrupt/cmdline error → `error_message` set, `killed_mutant=False`

**Status:** **Supported.** Prior to this fix, exit 5 was classified as `killed_mutant=True`, producing false kills for mutants in functions with no matching test files. The test-selection heuristic (grep for function name) can return an empty match, causing pytest to find no tests when invoked with a filtered file list. This now correctly yields `MutationStatus.error` via `_classify`.

---

## 11. Purity: Mutating Method Calls

**Current Behavior:** The purity detector now checks for in-place mutating method calls (`.append()`, `.extend()`, `.insert()`, `.remove()`, `.pop()`, `.clear()`, `.update()`, `.setdefault()`, `.add()`, `.discard()`, `.sort()`, `.reverse()`). Any call of the form `obj.method(...)` where `method` is in `_MUTATING_METHOD_NAMES` causes the function to be classified as impure.

**Impact (before fix):** Functions that mutated their arguments via in-place methods were incorrectly claimed to be pure, producing false purity invariants.

**Status:** **Supported.** The check requires a dotted call (`len(name_parts) > 1`) to avoid false positives on top-level functions that happen to share a name with a mutating method (e.g., a module-level `sort()` helper). Cross-module calls remain untraced (see §4).
