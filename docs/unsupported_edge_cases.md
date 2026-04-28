# Unsupported Edge Cases & Future Refinements

This document tracks identified architectural gaps and edge cases in the current Agon implementation (as of April 2026).

---

## 1. Monorepo Scaling & Sandbox Overhead

**Current Behavior:** `shutil.copytree` copies the entire project root. In a large monorepo, `detect_project_root` may find a marker high up in the directory tree (e.g., at the repo root). 

**Impact:** Every single mutant run copies the *entire* monorepo, including dozens of packages that are not relevant to the function being tested. This can lead to massive disk I/O overhead and multi-gigabyte temporary folder consumption.

**Future Fix:** Selective copy. Only copy files reachable from the target function's import graph.

---

## 2. Global State & Process Isolation

**Current Behavior:** `SandboxRunner` spawns a subprocess, but the sandbox environment (the temporary project copy) provides only filesystem isolation, not system-level resource or network isolation.

**Impact:**
- **Network Side Effects:** If a mutated function (especially in Phase 2) makes a network call, it will execute against the real network.
- **Persistent State:** If tests write to a database or a shared cache (e.g., Redis) that is not mocked, mutants can poison that state for subsequent runs.
- **Resource Exhaustion:** A mutant that introduces an infinite loop or a memory leak can crash the host machine if not properly capped by cgroups or namespaces.

**Future Fix:** Tiered sandboxing using `nsjail` (Linux) or `sandbox-exec` (macOS).

---

## 3. C-Extensions & Environment Parity

**Current Behavior:** The sandbox subprocess inherits the parent's environment (VIRTUAL_ENV, PATH, etc.).

**Impact:** If the user's environment relies on shared libraries (e.g., `.so` or `.dylib` files) located at absolute paths *inside* the project root, those paths will break inside the sandbox root unless they are handled by the copy/ignore logic.

---

## 4. Transitive Impurity across Modules

**Current Behavior:** Agon's `EigentestEngine` handles transitive purity only within a single file. 

**Impact:** If function `A` in `module_a.py` calls function `B` in `module_b.py`, and `B` is impure (e.g., calls `print`), `A` will currently be marked as **PURE** because the engine does not trace imports.

**Future Fix:** Implement a cross-module import resolver and propagate impurity status across the entire project call graph.

---

## 5. Non-Deterministic Assertions

**Current Behavior:** Mechanical extraction identifies `assert` statements as invariants.

**Impact:** If an assertion uses non-deterministic values (e.g., `assert time.time() > 0`), the invariant is technically correct but might be difficult for `mutagen` or `spectre` to verify reliably without mocking the clock.

---

## 6. Concurrent Mutation Collisions

**Current Behavior:** Each mutant run uses a unique `tempfile.TemporaryDirectory`.

**Status:** **Supported.** This design correctly allows multiple Agon processes to analyze the same project concurrently without collision. The only bottleneck is system I/O and CPU.
