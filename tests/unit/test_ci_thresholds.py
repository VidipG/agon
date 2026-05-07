"""
Unit tests for CI exit-code logic (_check_ci_thresholds).

These tests call the function directly so they are isolated from the full CLI
setup and from the pipeline engine.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import typer

from agon.cli import _check_ci_thresholds
from agon.models.schema import (
    AgonReport,
    Counterexample,
    Mutation,
    MutationOperator,
    MutationOperatorClass,
    MutationStatus,
    FunctionRef,
    Location,
    ReportSummary,
    Severity,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_report(
    mutation_score: float = 1.0,
    counterexamples: list[Counterexample] | None = None,
    mutations: list[Mutation] | None = None,
) -> AgonReport:
    summary = ReportSummary(
        mutation_score=mutation_score,
        mutations_generated=len(mutations or []),
        counterexamples_found=len(counterexamples or []),
    )
    return AgonReport(
        project="/tmp/proj",
        scope=["src/"],
        mutations=mutations or [],
        counterexamples=counterexamples or [],
        summary=summary,
    )


def _make_request(fail_under: float | None = None, config_path: Path | None = None):
    req = MagicMock()
    req.fail_under = fail_under
    req.config_path = config_path
    return req


def _make_counterexample(severity: Severity, cx_id: str = "cx1") -> Counterexample:
    return Counterexample(
        id=cx_id,
        mutation_id="m1",
        invariant_id="",
        input=None,
        expected=None,
        actual=None,
        mutant_output=None,
        oracle_agreement=None,
        reproducer_code="def test_stub(): pass",
        severity=severity,
    )


def _make_mutation(status: MutationStatus = MutationStatus.survived) -> Mutation:
    ref = FunctionRef(
        file="lib.py", name="f", line_start=1, line_end=3,
        signature="(x)", content_hash="abc",
    )
    return Mutation(
        id="m1",
        function_refs=[ref],
        target_invariants=[],
        operator=MutationOperator.arithmetic_swap,
        operator_class=MutationOperatorClass.mechanical,
        original_code="+",
        mutated_code="-",
        location=Location(line=2, col_start=5, col_end=6),
        status=status,
    )


# ---------------------------------------------------------------------------
# fail_under tests
# ---------------------------------------------------------------------------


class TestFailUnder:
    def test_no_fail_under_does_not_exit(self):
        report = _make_report(mutation_score=0.5, mutations=[_make_mutation()])
        request = _make_request(fail_under=None)
        # Should not raise
        _check_ci_thresholds(report, request)

    def test_score_above_threshold_does_not_exit(self):
        report = _make_report(mutation_score=0.9, mutations=[_make_mutation()])
        request = _make_request(fail_under=0.8)
        _check_ci_thresholds(report, request)

    def test_score_equal_threshold_does_not_exit(self):
        report = _make_report(mutation_score=0.8, mutations=[_make_mutation()])
        request = _make_request(fail_under=0.8)
        _check_ci_thresholds(report, request)

    def test_score_below_threshold_raises_exit(self):
        report = _make_report(mutation_score=0.5, mutations=[_make_mutation()])
        request = _make_request(fail_under=0.8)
        with pytest.raises(typer.Exit) as exc_info:
            _check_ci_thresholds(report, request)
        assert exc_info.value.exit_code == 1

    def test_fail_under_ignored_when_no_mutations(self):
        """--fail-under only applies when mutations were actually run."""
        report = _make_report(mutation_score=0.0, mutations=[])
        request = _make_request(fail_under=0.8)
        # No mutations → gate does not trigger
        _check_ci_thresholds(report, request)


# ---------------------------------------------------------------------------
# ci.fail_on severity tests
# ---------------------------------------------------------------------------


class TestCIFailOn:
    def test_no_counterexamples_does_not_exit(self):
        report = _make_report(counterexamples=[])
        request = _make_request()
        _check_ci_thresholds(report, request)

    def test_low_severity_cx_does_not_exit_by_default(self):
        """Default ci.fail_on is ["critical", "high"]; low should pass."""
        cx = _make_counterexample(Severity.low)
        report = _make_report(counterexamples=[cx])
        request = _make_request()
        _check_ci_thresholds(report, request)

    def test_medium_severity_cx_does_not_exit_by_default(self):
        cx = _make_counterexample(Severity.medium)
        report = _make_report(counterexamples=[cx])
        request = _make_request()
        _check_ci_thresholds(report, request)

    def test_high_severity_cx_exits_by_default(self):
        cx = _make_counterexample(Severity.high)
        report = _make_report(counterexamples=[cx])
        request = _make_request()
        with pytest.raises(typer.Exit) as exc_info:
            _check_ci_thresholds(report, request)
        assert exc_info.value.exit_code == 1

    def test_critical_severity_cx_exits_by_default(self):
        cx = _make_counterexample(Severity.critical)
        report = _make_report(counterexamples=[cx])
        request = _make_request()
        with pytest.raises(typer.Exit) as exc_info:
            _check_ci_thresholds(report, request)
        assert exc_info.value.exit_code == 1


# ---------------------------------------------------------------------------
# Both gates combined
# ---------------------------------------------------------------------------


class TestBothGates:
    def test_both_failing_exits_once(self):
        cx = _make_counterexample(Severity.high)
        report = _make_report(
            mutation_score=0.3,
            mutations=[_make_mutation()],
            counterexamples=[cx],
        )
        request = _make_request(fail_under=0.8)
        with pytest.raises(typer.Exit) as exc_info:
            _check_ci_thresholds(report, request)
        assert exc_info.value.exit_code == 1
