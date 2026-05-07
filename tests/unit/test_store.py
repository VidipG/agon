"""
Unit tests for report persistence (save/load) and incremental_filter.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from agon.adapters.python import PythonAdapter
from agon.eigentest.engine import EigentestEngine
from agon.models.schema import (
    AgonReport,
    Mutation,
    MutationOperator,
    MutationOperatorClass,
    MutationStatus,
    Location,
    ReportSummary,
)
from agon.store import incremental_filter, load_report, save_report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_report(**kwargs) -> AgonReport:
    defaults = dict(
        project="/tmp/proj",
        scope=["src/"],
        invariants=[],
        mutations=[],
        summary=ReportSummary(),
    )
    defaults.update(kwargs)
    return AgonReport(**defaults)


def _make_mutation(func_ref, status=MutationStatus.killed) -> Mutation:
    from agon.models.schema import FunctionRef
    return Mutation(
        id="abc123",
        function_refs=[func_ref],
        target_invariants=[],
        operator=MutationOperator.arithmetic_swap,
        operator_class=MutationOperatorClass.mechanical,
        original_code="+",
        mutated_code="-",
        location=Location(line=2, col_start=10, col_end=11),
        status=status,
    )


def _make_func_ref(file="lib.py", name="add", content_hash="aaa"):
    from agon.models.schema import FunctionRef
    return FunctionRef(
        file=file,
        name=name,
        line_start=1,
        line_end=3,
        signature="(a, b)",
        content_hash=content_hash,
    )


def _parse_funcs(source: str, file: str = "lib.py"):
    adapter = PythonAdapter()
    tree = adapter.parse(source)
    return adapter.get_functions(tree, file, source)


# ---------------------------------------------------------------------------
# save_report / load_report
# ---------------------------------------------------------------------------


class TestSaveLoad:
    def test_roundtrip(self, tmp_path: Path):
        report = _make_report(project=str(tmp_path))
        path = tmp_path / "report.json"
        save_report(report, path)
        loaded = load_report(path)
        assert loaded.project == str(tmp_path)
        assert loaded.schema_version == report.schema_version

    def test_creates_parent_dirs(self, tmp_path: Path):
        report = _make_report()
        path = tmp_path / "nested" / "dir" / "report.json"
        save_report(report, path)
        assert path.exists()

    def test_mutations_preserved(self, tmp_path: Path):
        ref = _make_func_ref()
        m = _make_mutation(ref, status=MutationStatus.survived)
        report = _make_report(mutations=[m])
        path = tmp_path / "r.json"
        save_report(report, path)
        loaded = load_report(path)
        assert len(loaded.mutations) == 1
        assert loaded.mutations[0].status == MutationStatus.survived

    def test_load_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            load_report(tmp_path / "nonexistent.json")

    def test_overwrite_existing(self, tmp_path: Path):
        path = tmp_path / "r.json"
        save_report(_make_report(project="first"), path)
        save_report(_make_report(project="second"), path)
        loaded = load_report(path)
        assert loaded.project == "second"


# ---------------------------------------------------------------------------
# incremental_filter
# ---------------------------------------------------------------------------


class TestIncrementalFilter:
    def _make_funcs(self, source: str, file: str = "lib.py"):
        return _parse_funcs(source, file)

    def test_unchanged_function_excluded_from_run(self, tmp_path: Path):
        source = "def add(a, b):\n    return a + b\n"
        funcs = self._make_funcs(source)
        ref = funcs[0].ref

        prior_mutation = _make_mutation(ref, status=MutationStatus.killed)
        prior = _make_report(mutations=[prior_mutation])

        # Build a pending mutation with the same function ref
        pending = _make_mutation(ref, status=MutationStatus.pending)
        pending = pending.model_copy(update={"id": "pending01"})

        funcs_to_run, muts_to_run, carried = incremental_filter(funcs, [pending], prior)

        # Function is unchanged → should not be re-run
        assert not any(f.ref.name == ref.name for f in funcs_to_run)
        # Prior result carried over
        assert any(m.status == MutationStatus.killed for m in carried)
        # Pending mutation dropped (its function is unchanged)
        assert not any(m.id == "pending01" for m in muts_to_run)

    def test_changed_function_included_in_run(self, tmp_path: Path):
        source_old = "def add(a, b):\n    return a + b\n"
        source_new = "def add(a, b):\n    return a + b + 1\n"  # body changed

        funcs_old = self._make_funcs(source_old)
        funcs_new = self._make_funcs(source_new)

        ref_old = funcs_old[0].ref
        prior_mutation = _make_mutation(ref_old, status=MutationStatus.killed)
        prior = _make_report(mutations=[prior_mutation])

        ref_new = funcs_new[0].ref
        pending = _make_mutation(ref_new, status=MutationStatus.pending)
        pending = pending.model_copy(update={"id": "new01"})

        funcs_to_run, muts_to_run, carried = incremental_filter(funcs_new, [pending], prior)

        # Body changed → content_hash differs → must be re-run
        assert any(f.ref.name == "add" for f in funcs_to_run)
        assert any(m.id == "new01" for m in muts_to_run)
        # Nothing carried over since the function changed
        assert not carried

    def test_new_function_included_in_run(self, tmp_path: Path):
        source = "def brand_new(x):\n    return x * 2\n"
        funcs = self._make_funcs(source)
        ref = funcs[0].ref

        # Prior report has no mutations for this function
        prior = _make_report(mutations=[])
        pending = _make_mutation(ref, status=MutationStatus.pending)
        pending = pending.model_copy(update={"id": "brand_new_01"})

        funcs_to_run, muts_to_run, carried = incremental_filter(funcs, [pending], prior)

        assert any(f.ref.name == "brand_new" for f in funcs_to_run)
        assert any(m.id == "brand_new_01" for m in muts_to_run)
        assert not carried

    def test_empty_prior_runs_everything(self, tmp_path: Path):
        source = "def f(x):\n    return x + 1\n"
        funcs = self._make_funcs(source)
        ref = funcs[0].ref
        pending = _make_mutation(ref, status=MutationStatus.pending)

        prior = _make_report(mutations=[])
        funcs_to_run, muts_to_run, carried = incremental_filter(funcs, [pending], prior)

        assert funcs_to_run == funcs
        assert muts_to_run == [pending]
        assert not carried

    def test_mixed_changed_and_unchanged(self, tmp_path: Path):
        src_unchanged = "def stable(x):\n    return x\n"
        src_changed = "def changed(x):\n    return x + 1\n"

        funcs_unchanged = _parse_funcs(src_unchanged, "a.py")
        funcs_changed_old = _parse_funcs(src_changed, "b.py")
        funcs_changed_new = _parse_funcs(
            "def changed(x):\n    return x + 99\n", "b.py"
        )

        all_funcs = funcs_unchanged + funcs_changed_new

        prior_m_stable = _make_mutation(funcs_unchanged[0].ref, MutationStatus.survived)
        prior_m_stable = prior_m_stable.model_copy(update={"id": "s1"})
        prior_m_changed = _make_mutation(funcs_changed_old[0].ref, MutationStatus.killed)
        prior_m_changed = prior_m_changed.model_copy(update={"id": "c1"})
        prior = _make_report(mutations=[prior_m_stable, prior_m_changed])

        pending_stable = _make_mutation(funcs_unchanged[0].ref, MutationStatus.pending)
        pending_stable = pending_stable.model_copy(update={"id": "ps1"})
        pending_changed = _make_mutation(funcs_changed_new[0].ref, MutationStatus.pending)
        pending_changed = pending_changed.model_copy(update={"id": "pc1"})

        funcs_to_run, muts_to_run, carried = incremental_filter(
            all_funcs, [pending_stable, pending_changed], prior
        )

        # stable function unchanged → carried over, not re-run
        assert not any(f.ref.name == "stable" for f in funcs_to_run)
        assert any(m.id == "s1" for m in carried)
        assert not any(m.id == "ps1" for m in muts_to_run)

        # changed function → re-run
        assert any(f.ref.name == "changed" for f in funcs_to_run)
        assert any(m.id == "pc1" for m in muts_to_run)
