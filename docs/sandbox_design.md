# Agon Sandbox Design

**Scope:** Execution isolation strategy for mutation testing in agon's `SandboxRunner`.

---

## 1. Problem Statement

Mutation testing requires executing the user's test suite hundreds of times — once per mutant — each time with a syntactically modified version of one source file. The execution model must satisfy three constraints simultaneously:

1. **Correctness:** The test subprocess must import the mutated version of the file, not the original.
2. **Source integrity:** The user's source tree must never be left in a corrupted or modified state, regardless of how agon terminates.
3. **Universality:** The mechanism must work for any Python project layout without requiring configuration.

These three constraints are in tension. The first requires the test runner to see a modified module. The second and third constrain how that modification is delivered.

---

## 2. Execution Context

`SandboxRunner` receives `Mutation` objects from `MutagenEngine`, each describing a single token-level change (operator swap, constant replacement, etc.) at a specific line and column in a specific source file. For each mutation:

1. `PythonAdapter.apply_mutation(func.source, mutation)` applies the change to the in-memory source string.
2. The resulting `mutated_source` string must be what the test subprocess imports.
3. `PythonAdapter.run_tests(project_root, test_filter, timeout_seconds)` spawns `pytest` as a subprocess.

The subprocess is a fresh Python interpreter. It inherits environment variables from the parent process and builds `sys.path` from scratch at startup. No module cache is shared between the parent agon process and the subprocess.

---

## 3. Candidate Approaches

### 3.1 In-Place File Swap

**Mechanism:** Read the original file into memory, overwrite it with the mutated content, run tests, restore via `try/finally`.

```python
original = path.read_bytes()
try:
    path.write_text(mutated_content)
    run_tests(project_root)
finally:
    path.write_bytes(original)
```

**Why it was the initial implementation:** Simple, universally compatible, requires no environment manipulation. The subprocess imports the mutated file because it is literally the file on disk.

**Why it was replaced:**

- `try/finally` cannot execute if the process receives `SIGKILL`, is terminated by the OOM killer, or experiences a power failure mid-write. In those cases the file on disk contains the mutated content. The user's source code is corrupted with no recovery path.
- Concurrent agon runs on the same project — e.g., in parallel CI jobs sharing a checkout — produce undefined behavior because the same file is modified by multiple processes.
- File system writes are not atomic on POSIX systems for general-purpose `open()`/`write()` calls. A crash between `write()` and `fsync()` can leave a partial or empty file.

### 3.2 PYTHONPATH Overlay

**Mechanism:** Create a temporary directory containing only the mutated file. Prepend that directory to `PYTHONPATH` before invoking `pytest`. Python's import system finds the mutated file first because `PYTHONPATH` entries precede the project's source directories in `sys.path`.

```
PYTHONPATH=/tmp/agon_mut_xxx:/path/to/project/src pytest tests/
```

**Apparent correctness:** At Python interpreter startup, `sys.path` is initialized in the following order:

1. The script directory (or `''` for interactive sessions).
2. Entries from `PYTHONPATH`, left to right.
3. Installation-dependent defaults (site-packages, `.pth` file additions).

If step 2 runs before step 3, and the project's source directory enters `sys.path` only via site-packages or a `.pth` file, then `/tmp/agon_mut_xxx` would appear earlier and the mutated module would be imported first.

**Why it fails in practice:**

pytest's default import mode (`prepend`) explicitly manipulates `sys.path` during test collection. When pytest imports a test file, it calls `sys.path.insert(0, basedir)` where `basedir` is the first ancestor of the test file that does not contain an `__init__.py`. For a project rooted at `/path/to/project` with tests at the top level, `basedir = /path/to/project`.

The resulting `sys.path` at test execution time is:

```
['/path/to/project',          # inserted by pytest at index 0
 '',                           # interpreter default
 '/tmp/agon_mut_xxx',         # from PYTHONPATH (now at index 2)
 '/path/to/project/src',      # from PYTHONPATH
 ...]
```

The original project root is at index 0. When the test module executes `from lib import add`, Python checks `sys.path` in order and finds `/path/to/project/lib.py` before `/tmp/agon_mut_xxx/lib.py`. The original file is imported. The mutation is silently not tested.

This failure is not conditional on project layout. It occurs for flat projects (single directory), src-layout projects, and namespace packages. The only exception is pytest's `importlib` mode (`--import-mode=importlib`), which does not modify `sys.path` for test collection. However, `importlib` mode does not affect how the test file's own `import` statements are resolved — those still use the `sys.path` state at runtime, which pytest may have modified before the test module was imported.

Relying on `--import-mode=importlib` is not an acceptable solution because: (a) it changes pytest's module isolation semantics in ways that can break test suites relying on default behavior; (b) it requires agon to inject `PYTEST_ADDOPTS` into the subprocess environment, which has its own ordering and override complications; (c) it does not compose with `conftest.py` files that call `sys.path.insert(0, ...)` directly.

**Verdict:** The PYTHONPATH overlay approach is not reliable for pytest. It would produce incorrect mutation scores by testing the original code instead of the mutant.

### 3.3 import Hook via sitecustomize.py

**Mechanism:** Write a `sitecustomize.py` to a temp directory and include that directory in `PYTHONPATH`. Python's site initialization imports `sitecustomize` early in the interpreter startup sequence, before any user code runs. The `sitecustomize.py` installs a `sys.meta_path` finder that intercepts `import` calls for the target module and returns the mutated source.

**Why it was not adopted:**

The `sys.meta_path` hook must be installed before pytest's `sys.path.insert(0, basedir)` call, which `sitecustomize.py` does satisfy. However, the implementation requires:

- Mapping the absolute file path of the mutated file to a Python module name. This is not always unambiguous: the same `.py` file can be importable under multiple names depending on which `sys.path` entry is used to resolve it (`lib`, `src.lib`, `mypackage.lib`). Choosing the wrong canonical name means the hook intercepts nothing.
- Writing the hook dynamically per mutant with the correct module name and file path embedded in the hook source.
- Ensuring no conflict with any existing `sitecustomize.py` in the user's environment.
- Handling namespace packages, `__init__.py`-less packages, and `importlib.util.spec_from_file_location` calls that bypass `sys.meta_path` entirely.

The implementation complexity is high, the failure modes are subtle, and any bug in the hook produces incorrect results silently (the wrong module is imported, tests run against the original code, and the mutation appears to survive). The project copy approach described in Section 4 achieves the same safety properties with none of this complexity.

### 3.4 git worktree

**Mechanism:** Use `git worktree add` to create a linked working tree at a temporary path, apply the mutation there, and run tests from the worktree.

**Why it was not adopted:**

- Requires the project to be a git repository. Projects without a `.git` directory — a common condition during development, in CI scratch directories, or for newly created projects — cannot use this approach.
- A `git worktree` shares the git object store with the main repository. Operations that modify the worktree's index (e.g., test fixtures that call `git` themselves) can corrupt the parent repository state.
- Creating and deleting a worktree is slower than writing to a temporary directory because it involves git internal operations (pack-file locking, reflog updates).
- The worktree must be cleaned up explicitly with `git worktree remove`. If agon is killed before cleanup, the worktree remains registered and the user must run `git worktree prune` to recover.

### 3.5 Container Sandboxing (Docker/Podman)

**Mechanism:** Spawn each test run inside an ephemeral container with the project mounted read-only and a writable overlay for the mutated file.

**Why it was not adopted:**

- **Startup latency:** Starting a Docker container takes 0.5–3 seconds on typical hardware, depending on image size and daemon state. With 500 mutants per analysis run, container overhead alone adds 4–25 minutes. Mutation testing latency is dominated by test execution time; adding container overhead would make agon unusably slow for most projects.
- **Docker-in-Docker:** Users running agon inside CI pipelines that are themselves containerized must configure Docker socket mounting or a Docker-in-Docker sidecar. This is a significant operational burden and a known security concern.
- **Platform dependency:** Rootless Podman, Docker Desktop on macOS, and Docker Engine on Linux have materially different performance and capability characteristics. A sandboxing strategy that depends on container runtime availability cannot be the default.
- **Overlay filesystem:** Mounting the project read-only and placing the mutated file in a writable layer requires either `COPY-on-WRITE` filesystem support (overlay2, AUFS) or a bind-mount scheme that varies by platform. Neither is transparently portable.

Container sandboxing is appropriate as an optional backend for high-security environments or when auditable execution records are required. The `SandboxConfig.backend` field in `AgonConfig` reserves this as a future `"container"` option.

### 3.6 Cloud Execution (Modal, AWS Lambda, Piston)

**Mechanism:** Serialize the project and test command, send it to a remote execution service, receive results.

**Why it was not adopted:**

- Requires network connectivity. agon is a CLI tool expected to work offline and in air-gapped CI environments.
- Introduces latency per mutant for serialization, network round-trip, and deserialization. At 50ms round-trip, 500 mutants cost 25 additional seconds in network time alone, before any execution overhead.
- Requires an API key and account with a third-party service. This creates an onboarding barrier, a cost dependency, and a privacy concern (user source code is transmitted to external infrastructure).
- Service-side Python runtime versions may not match the user's local environment, producing false positives where tests fail due to environment differences rather than the mutation.

Cloud execution is reserved as a future `"cloud"` backend option in `SandboxConfig`.

### 3.7 WASM (Pyodide / wasmtime-py)

**Mechanism:** Compile a Python interpreter to WebAssembly and execute the mutated code inside the WASM VM. The WASM runtime provides deterministic execution and no access to the host filesystem by default.

**Why it does not apply to agon:**

The WASM approach is appropriate for executing a single Python expression or function call in an isolated context. It is not applicable to agon because:

**Test suite execution requires a full pytest run.** agon's correctness criterion is whether the user's existing test suite kills a mutant. That test suite imports the project under test, uses pytest fixtures, may invoke `conftest.py` hooks, and may depend on any number of third-party packages. Running pytest inside a WASM VM requires:

1. A WASM build of CPython with the standard library.
2. WASM builds of pytest and all its dependencies (`pluggy`, `py`, `_pytest`, etc.).
3. WASM builds of all packages imported by the test suite.

Any package with a C extension — `pydantic` (Rust/C), `lxml`, `numpy`, `cryptography`, `tree-sitter` (which agon itself depends on) — cannot run inside WASM without a dedicated WASM compilation target for that package. The set of Python packages with WASM builds is small.

**The available WASM Python builds are incomplete and unmaintained.** The VMware WASM Labs distribution (`python-3.11.1+20230118-f23f3f3`) is the most-cited example of a self-contained Python WASM binary. VMware's WASM Labs team was dissolved following the Broadcom acquisition in 2024. No actively maintained, production-ready WASM Python distribution exists as of the writing of this document that supports a broad enough package ecosystem to run real test suites.

**Fuel-based CPU limits do not replace security isolation.** `wasmtime`'s fuel mechanism counts instruction executions and traps when the fuel budget is exhausted. This is useful for infinite-loop prevention in code-snippet execution but does not provide security isolation, filesystem protection, or network restriction when the WASM module is given filesystem access (via `WasiConfig.preopen_dir`).

**Pyodide in a browser context** is not applicable to a CLI tool that must run in a terminal subprocess.

WASM may become viable for agon's "pure-function micro-evaluation mode" — a future feature that would test individual function calls against a set of example inputs without invoking the full test suite. That use case is narrow enough to work within WASM's constraints. It is not viable as the general sandbox backend.

### 3.8 OS-Native Process Isolation (nsjail, sandbox-exec)

**Mechanism:** Wrap each `pytest` subprocess in an OS-level sandbox that restricts filesystem access, network access, and system call surface.

- Linux: `nsjail` (Google), `seccomp-bpf` + `unshare`, `bubblewrap`.
- macOS: `sandbox-exec` with a `.sb` profile.

**Why it was not adopted for Phase 1:**

- **Linux-only tools are not portable.** `nsjail` and `bubblewrap` are Linux-specific. macOS requires a separate sandboxing mechanism with a different policy language. A portable implementation requires maintaining two separate sandbox policy systems and detecting the platform at runtime.
- **`sandbox-exec` is deprecated on macOS.** Apple deprecated `sandbox-exec` in macOS 10.15 (Catalina). Its behavior on modern macOS versions is not guaranteed.
- **nsjail requires elevated privileges or kernel configuration.** `nsjail` needs either `CAP_SYS_ADMIN` or `user_namespaces` support, which is disabled or requires additional configuration in many restricted environments (corporate Linux machines with hardened kernels, some CI providers, Docker-based CI without `--privileged`).
- **Phase 1 threat model does not require process isolation.** Phase 1 mutations are mechanical token-level transformations (operator swaps, constant substitutions). These mutations cannot introduce new function calls, `import` statements, or I/O operations. A mutated `+` becoming `-` cannot `rm -rf` anything. The source integrity risk is accounted for by the copy sandbox. Process isolation provides meaningful security benefit only when the code under test is untrusted — which applies to Phase 2 (LLM-generated mutations), not Phase 1.

OS-native isolation is the appropriate evolution for Phase 2, where LLM-generated mutations could theoretically introduce side effects. It is documented here as the intended future direction for the `"process"` backend.

---

## 4. Chosen Approach: Project Copy Sandbox

### 4.1 Mechanism

For each mutant, `_copy_sandbox` creates a self-contained temporary copy of the project, writes the mutated content to the correct location within the copy, and yields the copy root as the working directory for `run_tests`.

```python
@contextmanager
def _copy_sandbox(
    project_root: Path,
    source_file: Path,
    mutated_content: str,
) -> Generator[Path, None, None]:
    rel = source_file.relative_to(project_root)
    with tempfile.TemporaryDirectory(prefix="agon_mut_") as tmp:
        sandbox_root = Path(tmp) / "project"
        shutil.copytree(project_root, sandbox_root, ignore=_COPY_IGNORE, symlinks=False)
        (sandbox_root / rel).write_text(mutated_content, encoding="utf-8")
        yield sandbox_root
```

`run_tests` is called with `sandbox_root` as the `project_root` argument, so `pytest` runs from inside the copy:

```python
with _copy_sandbox(project_root, source_file, mutated_source) as sandbox_root:
    test_result = self._adapter.run_tests(
        sandbox_root,
        test_filter=test_files,
        timeout_seconds=timeout,
    )
```

### 4.2 Why This Correctly Solves the Import Resolution Problem

When `pytest` runs from `sandbox_root`, its `prepend`-mode `sys.path.insert(0, basedir)` call inserts a path within `sandbox_root`, not within `project_root`. For a flat project:

```
sys.path[0] = sandbox_root      # e.g. /tmp/agon_mut_xxx/project/
```

The mutated file is at `sandbox_root/lib.py`. When the test module executes `from lib import add`, Python looks for `lib` in `sys.path` and finds `sandbox_root/lib.py` — the mutated version. No PYTHONPATH manipulation is needed.

For a src-layout project (source under `src/`):

```
sys.path[0] = sandbox_root/src/   # pytest inserts the rootdir, then site.py
                                   # processes the editable install pth file which
                                   # adds project_root/src — but that comes after
                                   # sandbox_root entries in sys.path order
```

Because pytest inserts `sandbox_root`-relative paths at position 0, and the editable install's `.pth` file adds `project_root/src` as a site-packages entry (position 3+), the sandbox copy's source is always found first.

**Key invariant:** pytest always inserts paths derived from its `rootdir`, which it computes relative to `cwd`. Since `pytest` is spawned with `cwd=sandbox_root`, all paths pytest inserts are within `sandbox_root`. The original `project_root` is not in `sys.path` unless it appears there via the editable install mechanism, which always comes after `sys.path.insert(0, ...)` entries.

### 4.3 Excluded Paths

`_copy_sandbox` uses `adapter.sandbox_ignore_patterns()` to determine which files to exclude from the copy. For Python, this includes standard patterns like `__pycache__` and `.venv`. For other languages like TypeScript, this would include `node_modules` (which should be symlinked rather than copied, as discussed in Section 4.7).

| Pattern | Reason |
|---|---|
| `*.pyc`, `__pycache__` | Compiled bytecodes would be stale and could shadow fresh source. |
| `.git` | git metadata is not needed and adds significant size. |
| `.venv`, `venv`, `env`, `node_modules` | Large environment/dependency folders that should not be copied. |
| `dist`, `build` | Build artifacts; not needed for test execution. |

---

### 4.7 Multi-Language Support

The `SandboxRunner` is designed to be language-agnostic by delegating language-specific logic to a `LanguageAdapter`:

1.  **Test Discovery:** The `select_tests` helper uses `adapter.test_file_patterns()` to find relevant test files (e.g., `test_*.py` for Python, `*.test.ts` for TypeScript).
2.  **Sandbox Isolation:** The `_copy_sandbox` context manager uses `adapter.sandbox_ignore_patterns()` to exclude unnecessary files.
3.  **Dependency Handling (Future):** For languages with heavy dependency folders (like TypeScript's `node_modules`), the adapter will eventually handle symlinking the original folder into the sandbox root to avoid massive copy overhead while maintaining import resolution.
4.  **Test Execution:** All test runs are performed via `adapter.run_tests()`, which encapsulates the specific test runner command (e.g., `pytest`, `jest`, `vitest`).

This abstraction allows Agon to support new languages by simply implementing a new `LanguageAdapter`, without changing the core execution and isolation logic.

### 4.4 Source Integrity Guarantee

The original `source_file` is never opened for writing. The mutation is applied to the in-memory `mutated_source` string (via `apply_mutation`) and written directly to `sandbox_root / rel`. If agon is killed at any point — including during `shutil.copytree`, during the `write_text` call on the copy, or while `pytest` is running — the user's `project_root` is unmodified.

`tempfile.TemporaryDirectory` creates the temp directory with the OS registering it for cleanup. On POSIX systems, the directory exists as an entry in the filesystem until it is explicitly removed. If the agon process is killed before `TemporaryDirectory.__exit__` is called, the temp directory will persist until the next OS reboot or manual cleanup. This is a resource leak, not a correctness issue. The user's source files remain intact.

Contrast with the in-place swap: if the agon process is killed after `path.write_text(mutated_content)` but before `path.write_bytes(original_bytes)`, the user's source file contains the mutated content with no indication or recovery path.

### 4.5 Baseline Verification

Before running any mutant for a given function, `SandboxRunner._check_baseline` runs the test suite against the original `project_root` (not a copy):

```python
baseline = self._adapter.run_tests(
    project_root,        # original, unmodified
    test_filter=test_files,
    timeout_seconds=timeout,
)
```

If the baseline fails, all mutations for that function are marked `error` and skipped. This prevents two categories of false results:

- **False kills:** If the baseline already fails (test infrastructure broken, missing fixture, etc.), any mutant would also "fail" the tests. Counting these as kills inflates the mutation score.
- **False survivors:** If the baseline fails due to a missing import or syntax error elsewhere in the module, the mutation might not be reachable at all. Counting these as survivors deflates the score.

The baseline runs against `project_root` rather than a copy for two reasons: (1) it is faster — no copy overhead; (2) it verifies the actual source state the user has on disk, which is the ground truth.

### 4.6 Test Selection

`select_tests` scans `project_root` for test files and matches them by token presence of the function's leaf name. It returns paths relative to `project_root`. When `run_tests` is called with `sandbox_root` and the same relative test file paths, the paths resolve correctly because `sandbox_root` has an identical directory structure.

The token-presence match uses a word-boundary regex to avoid false positives from substring matching (e.g., `foo` matching `test_foo_bar`). If no test files match, the full test suite is run — this is the safe fallback that avoids false survivors from empty test selections.

---

## 5. Performance Characteristics

**Copy overhead:** `shutil.copytree` copies all non-excluded files. For a project with:

- 50 Python source files averaging 200 lines: approximately 500 KB total, copy time < 5ms.
- 500 Python source files averaging 300 lines: approximately 7.5 MB total, copy time approximately 30–80ms.
- 5000 Python source files: approximately 75 MB total, copy time approximately 300–800ms.

For large projects (>1000 source files), copy overhead becomes significant relative to test execution time. The planned optimization is selective copy: instead of copying the entire tree, copy only the files reachable from the test file's imports. This requires a static import graph traversal and is reserved for a future phase.

**Test execution dominates:** For a typical function with a test that takes 50ms, the overhead of copying a 100-file project (~10ms) adds approximately 20% to the per-mutant cost. For tests taking 200ms (realistic for integration-style tests), the overhead is approximately 5%.

**No parallelism in Phase 1:** `MutagenConfig.parallel_workers` is set to 1. The copy sandbox is safe for parallel execution (each mutant gets an independent temp directory), but the current implementation is serial. Parallelism is deferred until the test runner is instrumented to report results incrementally rather than buffering all output.

---

## 6. Known Limitations

**Projects with external state:** If tests write to files in `project_root` or read configuration from paths hardcoded to `project_root`, they will operate on the copy's paths. Tests that verify file content written to `project_root` will fail because the copy has a different path. This is an edge case; well-written tests use `tmp_path` fixtures or relative paths.

**Editable installs with namespace packages:** PEP 420 namespace packages (no `__init__.py` at any level) are importable only via path hooks installed by site.py. If such a package is installed as an editable install pointing to `project_root`, the test subprocess will import the original namespace package from `project_root` via the `.pth` hook, even though the mutated copy is under `sandbox_root`. This is only an issue for namespace packages where the mutated file's package does not have `__init__.py` at any level. Standard packages with `__init__.py` are resolved via `sys.path` and behave correctly.

**Symlinks:** `shutil.copytree` with `symlinks=False` follows and dereferences symlinks. Symlinks within the project are materialized as regular files in the copy. Projects that use symlinks for deduplication (e.g., shared configuration files linked from multiple packages) will have those files duplicated in the copy, which is correct behavior for mutation testing purposes.

**Large generated files:** If `project_root` contains large generated `.py` files (e.g., protobuf-generated Python, ANTLR grammars), they are copied on every mutant run. The exclusion list does not currently handle generated-file directories. This can be addressed by extending `_COPY_IGNORE` or by adding a user-configurable `copy_ignore_patterns` field to `SandboxConfig`.

---

## 7. Configuration

The sandbox backend is selected via `AgonConfig.sandbox.backend`:

```toml
[sandbox]
backend = "process"   # current; "container" and "cloud" are reserved
```

Per-mutant timeout is computed from `GeneralConfig.timeout_seconds * MutagenConfig.timeout_multiplier`. The default is `30s * 2.0 = 60s`. A test that produces an infinite loop under mutation will be killed after 60 seconds and classified as `timeout`, not `killed` or `survived`.

---

## 8. Planned Evolution

**Phase 2 — LLM-generated mutations:** When mutations are generated by a language model rather than mechanical operator substitution, the mutated code may contain arbitrary Python, including `import os; os.system(...)`. The project copy approach still prevents source corruption, but does not prevent the subprocess from executing side effects. The planned response is an optional OS-native restriction layer:

- Linux: `seccomp-bpf` filter restricting the subprocess to a whitelist of syscalls (read, write, close, stat, mmap, futex, brk, exit); `CLONE_NEWNET` to remove network access.
- macOS: `sandbox-exec` with an `allow process*` + `deny network*` + `deny file-write* (subpath "/")`  + `allow file-write* (subpath (param "SANDBOX_ROOT"))` profile.

This is implemented as an additional wrapper around `subprocess.run` within `PythonAdapter.run_tests`, enabled only when `SandboxConfig.backend = "process"` and the LLM mutation path is active.

**Selective copy:** Replace full `copytree` with a targeted copy of only the import-reachable files from the relevant test file. Requires static import graph analysis (likely using `modulefinder` or `importlib.util.find_spec` traversal) and caching the graph across mutant runs for the same function.

**Coverage-guided test selection:** Use `pytest --cov-report=json` to identify which test lines execute the specific lines being mutated. This replaces the current token-presence heuristic and reduces false-negative test selections. Combined with selective copy, this provides the 10–100x speedup described in the Phase 1 plan for large projects.
