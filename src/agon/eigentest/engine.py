"""
Eigentest engine — orchestrates invariant inference for a set of paths.

Phase 0: mechanical extraction only (no LLM, no cache).
Phase 2: adds LLM inference chain and semantic cache.

Entry point:
    engine = EigentestEngine(adapter=PythonAdapter())
    result = engine.run(paths, functions_filter=None)
"""
from __future__ import annotations

from pathlib import Path

from ..adapters.base import FunctionNode, LanguageAdapter
from ..models.schema import FunctionRef, Invariant
from . import mechanical


class EigentestResult:
    """Output of a single eigentest run."""

    def __init__(
        self,
        functions: list[FunctionNode],
        invariants: list[Invariant],
    ) -> None:
        self.functions = functions
        self.invariants = invariants

    @property
    def invariants_by_function(self) -> dict[str, list[Invariant]]:
        result: dict[str, list[Invariant]] = {}
        for inv in self.invariants:
            for ref in inv.function_refs:
                result.setdefault(ref.name, []).append(inv)
        return result


class EigentestEngine:
    """Runs the eigentest invariant inference pipeline.

    Currently implements Phase 0: mechanical extraction only.
    LLM chain and semantic cache are plugged in during Phase 2.
    """

    def __init__(self, adapter: LanguageAdapter) -> None:
        self._adapter = adapter

    def run(
        self,
        paths: list[Path],
        *,
        functions_filter: list[str] | None = None,
        project_root: Path | None = None,
    ) -> EigentestResult:
        """Run eigentest on the given paths.

        Args:
            paths: Files or directories to analyze.
            functions_filter: If provided, only analyze these function names.
            project_root: Root for computing relative file paths. Defaults to cwd.
        """
        root = project_root or Path.cwd()
        all_functions: list[FunctionNode] = []

        for path in paths:
            if path.is_dir():
                py_files = sorted(path.rglob("*.py"))
            else:
                py_files = [path]

            for py_file in py_files:
                if not py_file.suffix == ".py":
                    continue
                source = py_file.read_text(encoding="utf-8")
                tree = self._adapter.parse(source)
                try:
                    rel = py_file.relative_to(root)
                except ValueError:
                    rel = py_file
                funcs = self._adapter.get_functions(tree, str(rel), source)
                all_functions.extend(funcs)

        # Apply function name filter if provided
        if functions_filter:
            filter_set = set(functions_filter)
            all_functions = [
                f for f in all_functions
                if f.ref.name in filter_set or f.ref.name.split(".")[-1] in filter_set
            ]

        all_invariants = _extract_invariants(all_functions)

        return EigentestResult(functions=all_functions, invariants=all_invariants)


def _extract_invariants(functions: list[FunctionNode]) -> list[Invariant]:
    """Run mechanical extraction with same-file transitive purity.

    Functions in the same file are grouped together so the purity pass can
    check whether a function calls a same-file callee that is already known
    to be impure.  Cross-file and cross-module calls are not traced — the
    conservative default (no purity claim) applies there.

    The pass is iterated until no new impure functions are discovered.
    One iteration normally suffices; chained impurity (A → B → C where C is
    impure) requires at most depth(chain) iterations.
    """
    # Group by file so same-file callees are visible to each other
    by_file: dict[str, list[FunctionNode]] = {}
    for func in functions:
        by_file.setdefault(func.ref.file, []).append(func)

    all_invariants: list[Invariant] = []

    for file_funcs in by_file.values():
        all_invariants.extend(_extract_with_transitive_purity(file_funcs))

    return all_invariants


def _extract_with_transitive_purity(file_funcs: list[FunctionNode]) -> list[Invariant]:
    """Extract invariants for one file, propagating impurity transitively."""
    # Determine which functions are directly impure (via I/O, global, etc.)
    # using an empty known_impure set first.
    known_impure: set[str] = set()
    changed = True
    while changed:
        changed = False
        for func in file_funcs:
            short_name = func.ref.name.split(".")[-1]
            if short_name in known_impure:
                continue
            # Tentatively extract with current known_impure set
            invs = mechanical.extract(func, known_impure=frozenset(known_impure))
            has_purity = any(
                inv.category.value == "purity" and "appears to be pure" in inv.property
                for inv in invs
            )
            if not has_purity:
                # This function is impure — record its leaf name so callers
                # that reference it by that name will also be marked impure
                known_impure.add(short_name)
                changed = True

    # Final extraction with the settled known_impure set
    invariants: list[Invariant] = []
    for func in file_funcs:
        invariants.extend(mechanical.extract(func, known_impure=frozenset(known_impure)))
    return invariants
