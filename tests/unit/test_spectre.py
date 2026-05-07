"""
Unit tests for SpectreEngine — mechanical counterexample generation.
"""
from __future__ import annotations

import pytest

from agon.models.schema import (
    Counterexample,
    FunctionRef,
    Invariant,
    InvariantCategory,
    InvariantSource,
    Location,
    Mutation,
    MutationOperator,
    MutationOperatorClass,
    MutationStatus,
    Severity,
)
from agon.spectre.engine import SpectreEngine, _OPERATOR_SEVERITY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ref(file: str = "lib.py", name: str = "add", content_hash: str = "aaa") -> FunctionRef:
    return FunctionRef(
        file=file,
        name=name,
        line_start=1,
        line_end=5,
        signature="(a, b)",
        content_hash=content_hash,
    )


def _make_mutation(
    ref: FunctionRef | None = None,
    operator: MutationOperator = MutationOperator.arithmetic_swap,
    status: MutationStatus = MutationStatus.survived,
    target_invariants: list[str] | None = None,
    mut_id: str = "mut001",
) -> Mutation:
    ref = ref or _make_ref()
    return Mutation(
        id=mut_id,
        function_refs=[ref],
        target_invariants=target_invariants or [],
        operator=operator,
        operator_class=MutationOperatorClass.mechanical,
        original_code="+",
        mutated_code="-",
        location=Location(line=3, col_start=10, col_end=11),
        status=status,
    )


def _make_invariant(inv_id: str = "inv001", func_name: str = "add") -> Invariant:
    ref = _make_ref(name=func_name)
    return Invariant(
        id=inv_id,
        function_refs=[ref],
        category=InvariantCategory.relational,
        property="result equals sum of inputs",
        property_code="result == a + b",
        confidence=0.9,
        source=InvariantSource.mechanical,
    )


# ---------------------------------------------------------------------------
# SpectreEngine.run
# ---------------------------------------------------------------------------


class TestSpectreEngine:
    def test_no_survived_mutations_returns_empty(self):
        engine = SpectreEngine()
        result = engine.run(survived_mutations=[], functions=[], invariants=[])
        assert result.counterexamples == []

    def test_survived_mutation_generates_counterexample(self):
        mut = _make_mutation()
        engine = SpectreEngine()
        result = engine.run([mut], functions=[], invariants=[])
        assert len(result.counterexamples) == 1
        cx = result.counterexamples[0]
        assert cx.mutation_id == mut.id

    def test_counterexample_links_to_invariant(self):
        inv = _make_invariant(inv_id="inv_abc")
        mut = _make_mutation(target_invariants=["inv_abc"])
        engine = SpectreEngine()
        result = engine.run([mut], functions=[], invariants=[inv])
        cx = result.counterexamples[0]
        assert cx.invariant_id == "inv_abc"

    def test_no_matching_invariant_still_generates_counterexample(self):
        mut = _make_mutation(target_invariants=["nonexistent"])
        engine = SpectreEngine()
        result = engine.run([mut], functions=[], invariants=[])
        assert len(result.counterexamples) == 1
        cx = result.counterexamples[0]
        assert cx.invariant_id == ""

    def test_multiple_mutations_generate_multiple_counterexamples(self):
        mut1 = _make_mutation(mut_id="m1")
        mut2 = _make_mutation(mut_id="m2", operator=MutationOperator.comparison_boundary)
        engine = SpectreEngine()
        result = engine.run([mut1, mut2], functions=[], invariants=[])
        assert len(result.counterexamples) == 2

    def test_counterexample_ids_are_unique(self):
        mut1 = _make_mutation(mut_id="m1")
        mut2 = _make_mutation(mut_id="m2")
        engine = SpectreEngine()
        result = engine.run([mut1, mut2], functions=[], invariants=[])
        ids = [cx.id for cx in result.counterexamples]
        assert len(ids) == len(set(ids))

    # Phase 1: input/expected/actual/mutant_output are deferred (None)
    def test_phase1_oracle_fields_are_none(self):
        mut = _make_mutation()
        engine = SpectreEngine()
        result = engine.run([mut], functions=[], invariants=[])
        cx = result.counterexamples[0]
        assert cx.input is None
        assert cx.expected is None
        assert cx.actual is None
        assert cx.mutant_output is None
        assert cx.oracle_agreement is None


# ---------------------------------------------------------------------------
# Severity mapping
# ---------------------------------------------------------------------------


class TestSeverityMapping:
    @pytest.mark.parametrize("operator,expected_severity", [
        (MutationOperator.exception_swallow, Severity.high),
        (MutationOperator.comparison_boundary, Severity.high),
        (MutationOperator.condition_negate, Severity.high),
        (MutationOperator.arithmetic_swap, Severity.high),
        (MutationOperator.boolean_negate, Severity.medium),
        (MutationOperator.return_value_replace, Severity.medium),
        (MutationOperator.statement_delete, Severity.medium),
        (MutationOperator.constant_replace, Severity.low),
    ])
    def test_operator_severity(self, operator: MutationOperator, expected_severity: Severity):
        assert _OPERATOR_SEVERITY[operator] == expected_severity

    def test_severity_reflected_in_counterexample(self):
        for operator, expected in _OPERATOR_SEVERITY.items():
            mut = _make_mutation(operator=operator)
            engine = SpectreEngine()
            result = engine.run([mut], functions=[], invariants=[])
            assert result.counterexamples[0].severity == expected, (
                f"operator {operator!r} should produce severity {expected!r}"
            )


# ---------------------------------------------------------------------------
# Reproducer code
# ---------------------------------------------------------------------------


class TestReproducerCode:
    def test_reproducer_is_non_empty(self):
        mut = _make_mutation()
        engine = SpectreEngine()
        result = engine.run([mut], functions=[], invariants=[])
        assert result.counterexamples[0].reproducer_code.strip()

    def test_reproducer_is_valid_python(self):
        """The generated reproducer stub must be parseable Python."""
        import ast
        mut = _make_mutation()
        engine = SpectreEngine()
        result = engine.run([mut], functions=[], invariants=[])
        reproducer = result.counterexamples[0].reproducer_code
        # Should not raise SyntaxError
        ast.parse(reproducer)

    def test_reproducer_contains_function_name(self):
        ref = _make_ref(name="compute_total")
        mut = _make_mutation(ref=ref)
        engine = SpectreEngine()
        result = engine.run([mut], functions=[], invariants=[])
        assert "compute_total" in result.counterexamples[0].reproducer_code

    def test_reproducer_contains_operator(self):
        mut = _make_mutation(operator=MutationOperator.comparison_boundary)
        engine = SpectreEngine()
        result = engine.run([mut], functions=[], invariants=[])
        assert "comparison_boundary" in result.counterexamples[0].reproducer_code

    def test_reproducer_includes_invariant_hint_when_linked(self):
        inv = _make_invariant(inv_id="i1")
        inv_with_hint = inv.model_copy(update={"property_code": "result >= 0"})
        mut = _make_mutation(target_invariants=["i1"])
        engine = SpectreEngine()
        result = engine.run([mut], functions=[], invariants=[inv_with_hint])
        assert "result >= 0" in result.counterexamples[0].reproducer_code

    def test_reproducer_valid_python_with_invariant(self):
        """Reproducer with invariant hint must also parse cleanly."""
        import ast
        inv = _make_invariant(inv_id="i2")
        mut = _make_mutation(target_invariants=["i2"])
        engine = SpectreEngine()
        result = engine.run([mut], functions=[], invariants=[inv])
        ast.parse(result.counterexamples[0].reproducer_code)
