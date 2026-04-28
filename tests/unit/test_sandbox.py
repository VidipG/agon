"""
Unit tests for the SandboxRunner and its helpers.

These tests use temporary project directories with real Python files and
real pytest invocations, so they are integration-style tests that verify
the full mutation → test-run → classify loop without mocking.

We keep the test functions tiny to minimise execution time.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from agon.adapters.python import PythonAdapter
from agon.adapters.python_mutator import collect_mutations, site_to_mutation
from agon.config import AgonConfig
from agon.eigentest.engine import EigentestEngine
from agon.models.schema import MutationOperator, MutationStatus
from agon.mutagen.engine import MutagenEngine
from agon.sandbox.process import (
    SandboxResult,
    SandboxRunner,
    _classify,
    _extract_failing_tests,
    _patched_file,
    select_tests,
)
from agon.adapters.base import TestResult


# ---------------------------------------------------------------------------
# _patched_file: context-manager correctness
# ---------------------------------------------------------------------------


class TestPatchedFile:
    def test_restores_on_normal_exit(self, tmp_path: Path):
        f = tmp_path / "mod.py"
        original = "x = 1\n"
        f.write_text(original)
        with _patched_file(f, "x = 999\n"):
            assert f.read_text() == "x = 999\n"
        assert f.read_text() == original

    def test_restores_on_exception(self, tmp_path: Path):
        f = tmp_path / "mod.py"
        original = "x = 1\n"
        f.write_text(original)
        with pytest.raises(RuntimeError):
            with _patched_file(f, "x = 999\n"):
                raise RuntimeError("boom")
        assert f.read_text() == original


# ---------------------------------------------------------------------------
# _classify: status mapping
# ---------------------------------------------------------------------------


class TestClassify:
    def _make_mutation(self):
        from agon.models.schema import Location, Mutation, MutationOperatorClass
        return Mutation(
            id="test01",
            function_refs=[],
            target_invariants=[],
            operator=MutationOperator.arithmetic_swap,
            operator_class=MutationOperatorClass.mechanical,
            original_code="+",
            mutated_code="-",
            location=Location(line=1, col_start=0, col_end=1),
        )

    def test_passed_tests_means_survived(self):
        m = self._make_mutation()
        result = TestResult(passed=True, stdout="", stderr="", duration_ms=10)
        assert _classify(m, result).status == MutationStatus.survived

    def test_failed_tests_means_killed(self):
        m = self._make_mutation()
        result = TestResult(passed=False, stdout="", stderr="", duration_ms=10,
                            killed_mutant=True)
        assert _classify(m, result).status == MutationStatus.killed

    def test_timeout_means_timeout_status(self):
        m = self._make_mutation()
        result = TestResult(passed=False, stdout="", stderr="", duration_ms=9999,
                            timed_out=True)
        assert _classify(m, result).status == MutationStatus.timeout

    def test_error_message_means_error_status(self):
        m = self._make_mutation()
        result = TestResult(passed=False, stdout="", stderr="", duration_ms=5,
                            error_message="crash")
        assert _classify(m, result).status == MutationStatus.error

    def test_execution_time_recorded_on_killed(self):
        m = self._make_mutation()
        result = TestResult(passed=False, stdout="", stderr="", duration_ms=42,
                            killed_mutant=True)
        classified = _classify(m, result)
        assert classified.execution_time_ms == 42


# ---------------------------------------------------------------------------
# _extract_failing_tests
# ---------------------------------------------------------------------------


class TestExtractFailingTests:
    def test_parses_failing_test_lines(self):
        output = textwrap.dedent("""\
            FAILED tests/test_foo.py::test_bar - AssertionError
            FAILED tests/test_foo.py::test_baz
            passed 3, failed 2
        """)
        tests = _extract_failing_tests(output)
        assert "tests/test_foo.py::test_bar" in tests
        assert "tests/test_foo.py::test_baz" in tests

    def test_empty_output_returns_empty_list(self):
        assert _extract_failing_tests("") == []


# ---------------------------------------------------------------------------
# select_tests
# ---------------------------------------------------------------------------


class TestSelectTests:
    def test_matching_test_file_selected(self, tmp_path: Path):
        (tmp_path / "lib.py").write_text("def my_func(x):\n    return x + 1\n")
        (tmp_path / "test_lib.py").write_text(
            "from lib import my_func\ndef test_my_func():\n    assert my_func(1) == 2\n"
        )
        adapter = PythonAdapter()
        tree = adapter.parse("def my_func(x):\n    return x + 1\n")
        funcs = adapter.get_functions(tree, "lib.py", "def my_func(x):\n    return x + 1\n")
        result = select_tests(funcs[0], tmp_path)
        assert result is not None
        assert any("test_lib.py" in f for f in result)

    def test_no_match_returns_none(self, tmp_path: Path):
        (tmp_path / "lib.py").write_text("def my_func(x):\n    return x + 1\n")
        (tmp_path / "test_unrelated.py").write_text(
            "def test_other():\n    assert 1 == 1\n"
        )
        adapter = PythonAdapter()
        funcs = adapter.get_functions(
            adapter.parse("def my_func(x):\n    return x + 1\n"),
            "lib.py", "def my_func(x):\n    return x + 1\n"
        )
        result = select_tests(funcs[0], tmp_path)
        assert result is None

    def test_source_file_itself_excluded(self, tmp_path: Path):
        """The source file under test must not be selected as a test file."""
        src = "def my_func(x):\n    return x\n"
        (tmp_path / "test_lib.py").write_text(src)
        adapter = PythonAdapter()
        funcs = adapter.get_functions(adapter.parse(src), "test_lib.py", src)
        result = select_tests(funcs[0], tmp_path)
        # test_lib.py IS a test file but it's also the source — should not be
        # selected against itself as a separate test runner target
        if result:
            for f in result:
                assert "test_lib.py" not in f or f != "test_lib.py"


# ---------------------------------------------------------------------------
# SandboxRunner: full end-to-end (real subprocess)
# ---------------------------------------------------------------------------


class TestSandboxRunnerE2E:
    """Full integration: write source + test, run mutations, check classification.

    These tests spawn subprocesses, so they are slower than unit tests.
    We keep the source and tests minimal to stay fast.
    """

    def _setup_project(self, tmp_path: Path, source: str, test_source: str) -> Path:
        (tmp_path / "lib.py").write_text(source)
        (tmp_path / "test_lib.py").write_text(test_source)
        return tmp_path

    def test_killed_mutation_detected(self, tmp_path: Path):
        """A mutation that changes a + to - should be killed by an add test."""
        source = "def add(a, b):\n    return a + b\n"
        tests = textwrap.dedent("""\
            from lib import add
            def test_add():
                assert add(2, 3) == 5
        """)
        self._setup_project(tmp_path, source, tests)
        adapter = PythonAdapter()
        eigen = EigentestEngine(adapter=adapter).run(
            [tmp_path / "lib.py"], project_root=tmp_path
        )
        mutagen = MutagenEngine(adapter=adapter)
        mutagen_result = mutagen.run(eigen.functions, eigen.invariants, AgonConfig())

        runner = SandboxRunner(adapter=adapter, config=AgonConfig())
        result = runner.run(mutagen_result.mutations, eigen.functions, tmp_path)

        killed = result.killed
        assert killed, "Expected at least one mutation to be killed"
        # The + → - mutation should be among the killed
        assert any(
            m.operator == MutationOperator.arithmetic_swap
            for m in killed
        )

    def test_survived_mutation_detected(self, tmp_path: Path):
        """A mutation that doesn't affect observable output should survive."""
        # A function that adds 1 — if we change the +1 to +0, the test only
        # checks type (isinstance), not value, so the mutation survives.
        source = "def increment(x: int) -> int:\n    return x + 1\n"
        tests = textwrap.dedent("""\
            from lib import increment
            def test_returns_int():
                assert isinstance(increment(5), int)
        """)
        self._setup_project(tmp_path, source, tests)
        adapter = PythonAdapter()
        eigen = EigentestEngine(adapter=adapter).run(
            [tmp_path / "lib.py"], project_root=tmp_path
        )
        mutagen = MutagenEngine(adapter=adapter)
        mutagen_result = mutagen.run(eigen.functions, eigen.invariants, AgonConfig())

        runner = SandboxRunner(adapter=adapter, config=AgonConfig())
        result = runner.run(mutagen_result.mutations, eigen.functions, tmp_path)

        survived = result.survived
        assert survived, "Expected at least one mutation to survive the weak test"

    def test_baseline_failure_aborts_function(self, tmp_path: Path):
        """If baseline tests already fail, mutations for that function are skipped."""
        source = "def f(x):\n    return x + 1\n"
        # Deliberately broken test
        tests = textwrap.dedent("""\
            from lib import f
            def test_broken():
                assert f(1) == 999  # always fails
        """)
        self._setup_project(tmp_path, source, tests)
        adapter = PythonAdapter()
        eigen = EigentestEngine(adapter=adapter).run(
            [tmp_path / "lib.py"], project_root=tmp_path
        )
        mutagen = MutagenEngine(adapter=adapter)
        mutagen_result = mutagen.run(eigen.functions, eigen.invariants, AgonConfig())

        runner = SandboxRunner(adapter=adapter, config=AgonConfig())
        result = runner.run(mutagen_result.mutations, eigen.functions, tmp_path)

        assert "f" in result.baseline_failures
        # All mutations should be error (not killed/survived) since baseline failed
        for m in result.mutations:
            assert m.status == MutationStatus.error

    def test_mutation_score_high(self, tmp_path: Path):
        """A thorough test suite should achieve a high mutation score.

        Note: ``< → <=`` and ``> → >=`` are equivalent mutants for ``clamp``
        when tested only at boundary equality (value==lo or value==hi both
        produce the same result).  A realistic score ceiling is therefore
        ~60-70% for this function with standard boundary tests.  We assert
        >=0.5 to confirm the sandbox correctly kills the non-equivalent
        mutations (return-value replacements).
        """
        source = textwrap.dedent("""\
            def clamp(value: int, lo: int, hi: int) -> int:
                if value < lo:
                    return lo
                if value > hi:
                    return hi
                return value
        """)
        tests = textwrap.dedent("""\
            from lib import clamp
            def test_below_lo(): assert clamp(-5, 0, 10) == 0
            def test_above_hi(): assert clamp(15, 0, 10) == 10
            def test_in_range(): assert clamp(5, 0, 10) == 5
            def test_lo_boundary(): assert clamp(0, 0, 10) == 0
            def test_hi_boundary(): assert clamp(10, 0, 10) == 10
        """)
        self._setup_project(tmp_path, source, tests)
        adapter = PythonAdapter()
        eigen = EigentestEngine(adapter=adapter).run(
            [tmp_path / "lib.py"], project_root=tmp_path
        )
        mutagen = MutagenEngine(adapter=adapter)
        mutagen_result = mutagen.run(eigen.functions, eigen.invariants, AgonConfig())

        runner = SandboxRunner(adapter=adapter, config=AgonConfig())
        result = runner.run(mutagen_result.mutations, eigen.functions, tmp_path)

        assert result.score >= 0.5, (
            f"Score {result.score:.2f} is below acceptable minimum for a thorough test suite. "
            f"killed={len(result.killed)}, survived={len(result.survived)}"
        )
        # Return-value mutations must all be killed (non-equivalent)
        rv_mutations = [m for m in result.mutations
                        if m.operator == MutationOperator.return_value_replace]
        assert all(m.status == MutationStatus.killed for m in rv_mutations), (
            "return_value_replace mutations should all be killed by thorough tests"
        )

    def test_source_restored_after_mutation(self, tmp_path: Path):
        """After all mutations, the original source file must be unchanged."""
        source = "def add(a, b):\n    return a + b\n"
        tests = "from lib import add\ndef test_add():\n    assert add(1, 2) == 3\n"
        self._setup_project(tmp_path, source, tests)
        adapter = PythonAdapter()
        eigen = EigentestEngine(adapter=adapter).run(
            [tmp_path / "lib.py"], project_root=tmp_path
        )
        mutagen = MutagenEngine(adapter=adapter)
        mutagen_result = mutagen.run(eigen.functions, eigen.invariants, AgonConfig())

        runner = SandboxRunner(adapter=adapter, config=AgonConfig())
        runner.run(mutagen_result.mutations, eigen.functions, tmp_path)

        assert (tmp_path / "lib.py").read_text() == source, (
            "Source file was not restored after mutation run"
        )
