"""
Spectre phase — mechanical counterexample generation.

Phase 1: Template-based reproducer generation for survived mutations.
         No LLM, no dynamic execution.

         For each survived mutation, produces a Counterexample with:
         - A severity derived from the mutation operator
         - A pytest test stub as reproducer_code (human-reviewable starting point)
         - Links to the mutation and the invariant it was designed to violate

Phase 3+ (future): LLM-powered input synthesis + dynamic execution to fill in
                   concrete input / expected / actual / mutant_output values.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from ..adapters.base import FunctionNode
from ..models.schema import (
    Counterexample,
    Invariant,
    Mutation,
    MutationOperator,
    Severity,
)

# ---------------------------------------------------------------------------
# Severity table
# ---------------------------------------------------------------------------

_OPERATOR_SEVERITY: dict[MutationOperator, Severity] = {
    MutationOperator.exception_swallow: Severity.high,
    MutationOperator.comparison_boundary: Severity.high,
    MutationOperator.condition_negate: Severity.high,
    MutationOperator.arithmetic_swap: Severity.high,
    MutationOperator.boolean_negate: Severity.medium,
    MutationOperator.return_value_replace: Severity.medium,
    MutationOperator.statement_delete: Severity.medium,
    MutationOperator.constant_replace: Severity.low,
}


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


@dataclass
class SpectreResult:
    counterexamples: list[Counterexample] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class SpectreEngine:
    """Generate counterexamples for survived mutations (Phase 1: mechanical).

    For each survived mutation the engine:
      1. Determines the severity from the mutation operator.
      2. Links the mutation to the invariant it was designed to violate (if any).
      3. Renders a pytest test stub as the reproducer_code — a developer can
         copy this and fill in concrete inputs to turn it into a real regression
         test.

    Phase 3 will replace step 3 with LLM-powered input synthesis plus dynamic
    execution of both the original and mutant to produce concrete values for
    ``input``, ``expected``, ``actual``, and ``mutant_output``.
    """

    def run(
        self,
        survived_mutations: list[Mutation],
        functions: list[FunctionNode],
        invariants: list[Invariant],
    ) -> SpectreResult:
        if not survived_mutations:
            return SpectreResult()

        inv_by_id: dict[str, Invariant] = {inv.id: inv for inv in invariants}
        fn_by_key: dict[tuple[str, str], FunctionNode] = {
            (fn.ref.file, fn.ref.name): fn for fn in functions
        }

        counterexamples = []
        for mutation in survived_mutations:
            cx = self._make_counterexample(mutation, inv_by_id, fn_by_key)
            if cx is not None:
                counterexamples.append(cx)

        return SpectreResult(counterexamples=counterexamples)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_counterexample(
        self,
        mutation: Mutation,
        inv_by_id: dict[str, Invariant],
        fn_by_key: dict[tuple[str, str], FunctionNode],
    ) -> Counterexample | None:
        # First invariant from the mutation's target list that exists in the report
        invariant: Invariant | None = None
        for inv_id in mutation.target_invariants:
            if inv_id in inv_by_id:
                invariant = inv_by_id[inv_id]
                break

        ref = mutation.function_refs[0] if mutation.function_refs else None
        fn = fn_by_key.get((ref.file, ref.name)) if ref else None

        severity = _OPERATOR_SEVERITY.get(mutation.operator, Severity.medium)
        reproducer = self._generate_reproducer(mutation, invariant, fn)

        cx_id = hashlib.sha256(
            f"{mutation.id}|{invariant.id if invariant else 'none'}".encode()
        ).hexdigest()[:16]

        return Counterexample(
            id=cx_id,
            mutation_id=mutation.id,
            invariant_id=invariant.id if invariant else "",
            input=None,
            expected=None,
            actual=None,
            mutant_output=None,
            oracle_agreement=None,
            reproducer_code=reproducer,
            severity=severity,
        )

    def _generate_reproducer(
        self,
        mutation: Mutation,
        invariant: Invariant | None,
        fn: FunctionNode | None,
    ) -> str:
        """Render a pytest test stub for the survived mutation."""
        ref = mutation.function_refs[0] if mutation.function_refs else None
        func_name = ref.name.split(".")[-1] if ref else "unknown"
        file_line = f"{ref.file}:{mutation.location.line}" if ref else "?:?"
        signature = ref.signature if ref else "(...)"

        # Construct a safe test function name
        raw_name = f"test_{func_name}_{mutation.operator.value}_{mutation.id[:6]}"
        test_name = raw_name.replace("-", "_").replace(".", "_")

        lines = [
            f"def {test_name}():",
            f"    # Survived mutation: {mutation.operator.value}",
            f"    #   Location:  {file_line}",
            f"    #   Original:  {mutation.original_code!r}",
            f"    #   Mutated:   {mutation.mutated_code!r}",
        ]

        if invariant:
            lines += [
                "    #",
                f"    # Violated invariant: {invariant.property}",
                f"    #   Assertion hint:   {invariant.property_code}",
            ]

        lines += [
            "    #",
            f"    # TODO: choose inputs that exercise {func_name}{signature}",
            f"    result = {func_name}(...)  # replace ... with concrete inputs",
            "    # TODO: assert the expected behavior",
        ]

        if invariant:
            lines.append(
                f"    # assert {invariant.property_code}  # invariant hint"
            )

        return "\n".join(lines)
