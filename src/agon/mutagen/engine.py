"""
MutagenEngine — orchestrates Phase 1 mechanical mutation generation.

Responsibilities
----------------
1. Receive list[FunctionNode] + list[Invariant] + AgonConfig from the pipeline.
2. Prioritise functions: auth/security/crypto paths come first (from PriorityConfig).
3. Skip functions that match skip_patterns (test files, __pycache__, etc.).
4. Call the Python mutator (or the appropriate language adapter) for each function.
5. Cap the per-function mutant count at MutagenConfig.max_mutants_per_function.
6. Link each Mutation to the Invariants it is designed to probe.
7. Deduplicate across functions: same (file, line, col, mutated) is one mutant.
8. Return MutagenResult — a plain value object with all pending Mutation objects.

No subprocess execution happens here; that is the SandboxRunner's responsibility.
"""
from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field

from ..adapters.base import FunctionNode, LanguageAdapter
from ..config import AgonConfig
from ..models.schema import (
    Invariant,
    InvariantCategory,
    Mutation,
    MutationOperator,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class MutagenResult:
    """Output of a single MutagenEngine run.

    ``mutations`` contains all pending Mutation objects, ordered by priority
    (auth/security functions first) then by (file, line).

    ``functions_skipped`` names functions that were excluded (generators,
    pattern-matched skips, zero mutations found).
    """

    mutations: list[Mutation] = field(default_factory=list)
    functions_skipped: list[str] = field(default_factory=list)

    @property
    def pending_count(self) -> int:
        return len(self.mutations)


# ---------------------------------------------------------------------------
# Invariant-category → operator mapping
# ---------------------------------------------------------------------------

# When a mutation operator is applied, these invariant categories are the most
# likely to be violated (and therefore the most relevant tests to run).
_OPERATOR_TO_CATEGORIES: dict[MutationOperator, set[InvariantCategory]] = {
    MutationOperator.comparison_boundary: {
        InvariantCategory.value_domain,
        InvariantCategory.relational,
        InvariantCategory.precondition,
        InvariantCategory.postcondition,
    },
    MutationOperator.boolean_negate: {
        InvariantCategory.precondition,
        InvariantCategory.postcondition,
        InvariantCategory.exception,
    },
    MutationOperator.arithmetic_swap: {
        InvariantCategory.relational,
        InvariantCategory.value_domain,
        InvariantCategory.postcondition,
    },
    MutationOperator.constant_replace: {
        InvariantCategory.value_domain,
        InvariantCategory.type_constraint,
        InvariantCategory.precondition,
    },
    MutationOperator.return_value_replace: {
        InvariantCategory.postcondition,
        InvariantCategory.type_constraint,
        InvariantCategory.value_domain,
    },
    MutationOperator.condition_negate: {
        InvariantCategory.precondition,
        InvariantCategory.postcondition,
        InvariantCategory.relational,
    },
    MutationOperator.exception_swallow: {
        InvariantCategory.exception,
        InvariantCategory.postcondition,
    },
    MutationOperator.statement_delete: {
        InvariantCategory.postcondition,
        InvariantCategory.relational,
        InvariantCategory.value_domain,
    },
}


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class MutagenEngine:
    """Generates Mutation objects for a list of FunctionNodes.

    The engine is stateless between calls to ``run()``; create a fresh engine
    per pipeline invocation or reuse it — both are safe.
    """

    def __init__(self, adapter: LanguageAdapter) -> None:
        self._adapter = adapter

    def run(
        self,
        functions: list[FunctionNode],
        invariants: list[Invariant],
        config: AgonConfig,
    ) -> MutagenResult:
        """Generate all pending mutations for *functions*.

        Args:
            functions: Functions discovered by EigentestEngine.
            invariants: Invariants inferred by EigentestEngine; used to link
                        each Mutation to the invariants it probes.
            config: Full AgonConfig (mutagen + priority sub-configs used).

        Returns:
            MutagenResult with all pending Mutation objects.
        """
        mutagen_cfg = config.mutagen
        priority_cfg = config.priority

        # Build invariant index: function qualified-name → list[Invariant]
        inv_by_func: dict[str, list[Invariant]] = {}
        for inv in invariants:
            for ref in inv.function_refs:
                inv_by_func.setdefault(ref.name, []).append(inv)

        # Separate critical vs normal functions
        critical_funcs, normal_funcs = _partition_by_priority(
            functions,
            critical_patterns=priority_cfg.critical_patterns,
            skip_patterns=mutagen_cfg.skip_patterns,
        )

        result = MutagenResult()
        # Deduplicate across all functions by (file, line, col_start, col_end, mutated)
        seen_global: set[tuple[str, int, int, int, str]] = set()

        for func in critical_funcs + normal_funcs:
            self._process_function(
                func,
                inv_by_func.get(func.ref.name, []),
                mutagen_cfg.max_mutants_per_function,
                result,
                seen_global,
            )

        return result

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _process_function(
        self,
        func: FunctionNode,
        func_invariants: list[Invariant],
        max_mutants: int,
        result: MutagenResult,
        seen_global: set[tuple[str, int, int, int, str]],
    ) -> None:
        mutations = self._adapter.collect_mutations(func)

        if not mutations:
            result.functions_skipped.append(func.ref.name)
            return

        # Cap per-function count; keep deterministic order (stable sort by line/col)
        # We sort the list of Mutation objects by their location
        mutations = sorted(
            mutations, 
            key=lambda m: (m.location.line, m.location.col_start)
        )[:max_mutants]

        added = 0
        for m in mutations:
            dedup_key = (
                func.ref.file,
                m.location.line,
                m.location.col_start,
                m.location.col_end,
                m.mutated_code,
            )
            if dedup_key in seen_global:
                continue
            seen_global.add(dedup_key)

            m = _link_invariants(m, func_invariants)
            result.mutations.append(m)
            added += 1

        if added == 0:
            result.functions_skipped.append(func.ref.name)
        else:
            logger.debug(
                "mutagen: %s → %d mutations (%d before cap/dedup)",
                func.ref.name,
                added,
                len(mutations),
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _partition_by_priority(
    functions: list[FunctionNode],
    critical_patterns: list[str],
    skip_patterns: list[str],
) -> tuple[list[FunctionNode], list[FunctionNode]]:
    """Split functions into (critical, normal), dropping skipped ones.

    ``critical_patterns`` and ``skip_patterns`` are glob patterns matched
    against ``FunctionRef.file`` (relative path from project root).
    """
    critical: list[FunctionNode] = []
    normal: list[FunctionNode] = []

    for func in functions:
        file_path = func.ref.file

        if _matches_any(file_path, skip_patterns):
            logger.debug("mutagen: skipping %s (matches skip_patterns)", func.ref.name)
            continue

        if _matches_any(file_path, critical_patterns):
            critical.append(func)
        else:
            normal.append(func)

    return critical, normal


def _matches_any(path: str, patterns: list[str]) -> bool:
    """Return True if *path* matches any of the given glob patterns.

    Supports ``**`` prefix patterns (``**/foo_*.py``) by also checking each
    suffix of the path, so both ``test_lib.py`` and ``src/test_lib.py`` are
    caught by ``**/test_*.py``.
    """
    from pathlib import PurePath

    p = PurePath(path)
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
        # Handle **/suffix patterns: check every suffix of the path parts
        if pattern.startswith("**/"):
            remainder = pattern[3:]
            for i in range(len(p.parts)):
                candidate = str(PurePath(*p.parts[i:])) if len(p.parts) > 1 else p.parts[0]
                if fnmatch.fnmatch(candidate, remainder):
                    return True
    return False


def _link_invariants(mutation: Mutation, func_invariants: list[Invariant]) -> Mutation:
    """Return a copy of *mutation* with target_invariants populated.

    Selects invariants whose category is in the operator's target set.
    """
    target_categories = _OPERATOR_TO_CATEGORIES.get(mutation.operator, set())
    target_ids = [
        inv.id
        for inv in func_invariants
        if inv.category in target_categories
    ]
    if not target_ids:
        return mutation
    return mutation.model_copy(update={"target_invariants": target_ids})
