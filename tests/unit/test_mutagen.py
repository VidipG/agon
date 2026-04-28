"""
Unit tests for the mutagen phase: mutation operators, MutagenEngine, and
the SandboxRunner (with lightweight subprocess-free doubles).
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from agon.adapters.python import PythonAdapter
from agon.adapters.python_mutator import (
    MutationSite,
    _apply_site,
    _is_generator,
    _is_valid_mutation,
    collect_mutations,
    site_to_mutation,
)
from agon.config import AgonConfig
from agon.eigentest.engine import EigentestEngine
from agon.models.schema import MutationOperator, MutationStatus
from agon.mutagen.engine import MutagenEngine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def adapter() -> PythonAdapter:
    return PythonAdapter()


@pytest.fixture
def engine(adapter: PythonAdapter) -> EigentestEngine:
    return EigentestEngine(adapter=adapter)


@pytest.fixture
def mutagen(adapter: PythonAdapter) -> MutagenEngine:
    return MutagenEngine(adapter=adapter)


def _parse_funcs(adapter: PythonAdapter, source: str, file: str = "test.py"):
    tree = adapter.parse(source)
    return adapter.get_functions(tree, file, source)


# ---------------------------------------------------------------------------
# _apply_site correctness
# ---------------------------------------------------------------------------


class TestApplySite:
    def test_arithmetic_operator(self):
        # "    return 1 + 2"  →  + is at col 13 (0-indexed)
        source = "def f():\n    return 1 + 2\n"
        site = MutationSite(line=2, col_start=13, col_end=14, original="+", mutated="-",
                            operator=MutationOperator.arithmetic_swap)
        result = _apply_site(source, site)
        assert "1 - 2" in result

    def test_comparison_operator(self):
        source = "def f(x):\n    return x > 0\n"
        # locate '>' position
        funcs = _parse_funcs(PythonAdapter(), source)
        sites = collect_mutations(funcs[0])
        gt_site = next(s for s in sites if s.operator == MutationOperator.comparison_boundary
                       and s.original == ">")
        mutated = _apply_site(source, gt_site)
        assert ">=" in mutated

    def test_out_of_range_line_is_noop(self):
        source = "x = 1\n"
        site = MutationSite(line=99, col_start=0, col_end=1, original="x", mutated="y",
                            operator=MutationOperator.constant_replace)
        assert _apply_site(source, site) == source


# ---------------------------------------------------------------------------
# collect_mutations: operator coverage
# ---------------------------------------------------------------------------


class TestCollectMutations:
    def _sites_by_op(self, source: str) -> dict[str, list[MutationSite]]:
        funcs = _parse_funcs(PythonAdapter(), source)
        sites = collect_mutations(funcs[0])
        result: dict[str, list[MutationSite]] = {}
        for s in sites:
            result.setdefault(s.operator.value, []).append(s)
        return result

    def test_arithmetic_swap_plus_to_minus(self):
        source = "def f(a, b):\n    return a + b\n"
        by_op = self._sites_by_op(source)
        assert MutationOperator.arithmetic_swap.value in by_op
        arith = by_op[MutationOperator.arithmetic_swap.value]
        assert any(s.original == "+" and s.mutated == "-" for s in arith)

    def test_arithmetic_swap_minus_to_plus(self):
        source = "def f(a, b):\n    return a - b\n"
        by_op = self._sites_by_op(source)
        arith = by_op.get(MutationOperator.arithmetic_swap.value, [])
        assert any(s.original == "-" and s.mutated == "+" for s in arith)

    def test_comparison_boundary_gt_to_gte(self):
        source = "def f(x):\n    return x > 0\n"
        by_op = self._sites_by_op(source)
        comp = by_op.get(MutationOperator.comparison_boundary.value, [])
        assert any(s.original == ">" and s.mutated == ">=" for s in comp)

    def test_comparison_boundary_eq_to_neq(self):
        source = "def f(x):\n    return x == 5\n"
        by_op = self._sites_by_op(source)
        comp = by_op.get(MutationOperator.comparison_boundary.value, [])
        assert any(s.original == "==" and s.mutated == "!=" for s in comp)

    def test_boolean_negate_and_to_or(self):
        source = "def f(a, b):\n    return a and b\n"
        by_op = self._sites_by_op(source)
        bools = by_op.get(MutationOperator.boolean_negate.value, [])
        assert any(s.original == "and" and s.mutated == "or" for s in bools)

    def test_boolean_negate_or_to_and(self):
        source = "def f(a, b):\n    return a or b\n"
        by_op = self._sites_by_op(source)
        bools = by_op.get(MutationOperator.boolean_negate.value, [])
        assert any(s.original == "or" and s.mutated == "and" for s in bools)

    def test_constant_replace_true_false(self):
        source = "def f():\n    return True\n"
        by_op = self._sites_by_op(source)
        const = by_op.get(MutationOperator.constant_replace.value, [])
        assert any(s.original == "True" and s.mutated == "False" for s in const)

    def test_constant_replace_integer(self):
        source = "def f():\n    return 0\n"
        by_op = self._sites_by_op(source)
        const = by_op.get(MutationOperator.constant_replace.value, [])
        assert any(s.original == "0" and s.mutated == "1" for s in const)

    def test_constant_replace_nonzero_integer_decrements(self):
        source = "def f():\n    return 5\n"
        by_op = self._sites_by_op(source)
        const = by_op.get(MutationOperator.constant_replace.value, [])
        assert any(s.original == "5" and s.mutated == "4" for s in const)

    def test_constant_replace_string(self):
        source = 'def f():\n    return "hello"\n'
        by_op = self._sites_by_op(source)
        const = by_op.get(MutationOperator.constant_replace.value, [])
        assert any(s.original == '"hello"' and s.mutated == '""' for s in const)

    def test_return_value_replace(self):
        source = "def f(x, y):\n    return x + y\n"
        by_op = self._sites_by_op(source)
        ret = by_op.get(MutationOperator.return_value_replace.value, [])
        assert any(s.mutated == "None" for s in ret)

    def test_return_none_not_replaced(self):
        """return None should not generate a return_value_replace mutation."""
        source = "def f():\n    return None\n"
        by_op = self._sites_by_op(source)
        ret = by_op.get(MutationOperator.return_value_replace.value, [])
        assert not ret

    def test_no_duplicate_sites(self):
        source = "def f(x):\n    return x > 0\n"
        funcs = _parse_funcs(PythonAdapter(), source)
        sites = collect_mutations(funcs[0])
        keys = [(s.line, s.col_start, s.col_end, s.mutated) for s in sites]
        assert len(keys) == len(set(keys)), "Duplicate mutation sites found"

    def test_all_mutations_produce_valid_syntax(self):
        """Every collected mutation must produce parseable Python."""
        source = textwrap.dedent("""\
            def process(items, threshold=0):
                result = []
                for item in items:
                    if item > threshold and item != 0:
                        result.append(item + 1)
                return result
        """)
        funcs = _parse_funcs(PythonAdapter(), source)
        sites = collect_mutations(funcs[0])
        assert sites, "Expected at least one mutation site"
        for site in sites:
            assert _is_valid_mutation(source, site), (
                f"Invalid mutation: {site.operator} {site.original!r} → {site.mutated!r} "
                f"at L{site.line}:{site.col_start}-{site.col_end}"
            )

    def test_mutations_scoped_to_function_range(self):
        """Mutations on one function must not bleed into a sibling function."""
        source = textwrap.dedent("""\
            def foo(x):
                return x + 1

            def bar(y):
                return y - 1
        """)
        adapter = PythonAdapter()
        tree = adapter.parse(source)
        funcs = adapter.get_functions(tree, "mod.py", source)
        foo = next(f for f in funcs if f.ref.name == "foo")
        bar = next(f for f in funcs if f.ref.name == "bar")

        foo_sites = collect_mutations(foo)
        bar_sites = collect_mutations(bar)

        foo_lines = {s.line for s in foo_sites}
        bar_lines = {s.line for s in bar_sites}
        assert not (foo_lines & bar_lines), "Mutation sites bleed between functions"


# ---------------------------------------------------------------------------
# Generator / special-form skipping
# ---------------------------------------------------------------------------


class TestSkipping:
    def test_generator_function_skipped(self):
        source = "def gen(n):\n    for i in range(n):\n        yield i\n"
        funcs = _parse_funcs(PythonAdapter(), source)
        assert _is_generator(funcs[0])
        assert collect_mutations(funcs[0]) == []

    def test_regular_function_not_skipped(self):
        source = "def f(x):\n    return x + 1\n"
        funcs = _parse_funcs(PythonAdapter(), source)
        assert not _is_generator(funcs[0])
        assert collect_mutations(funcs[0])

    def test_async_function_mutated(self):
        source = "async def f(x):\n    return x + 1\n"
        funcs = _parse_funcs(PythonAdapter(), source)
        sites = collect_mutations(funcs[0])
        assert any(s.operator == MutationOperator.arithmetic_swap for s in sites)


# ---------------------------------------------------------------------------
# site_to_mutation
# ---------------------------------------------------------------------------


class TestSiteToMutation:
    def test_deterministic_id(self):
        source = "def f(x):\n    return x + 1\n"
        funcs = _parse_funcs(PythonAdapter(), source)
        sites = collect_mutations(funcs[0])
        m1 = site_to_mutation(sites[0], funcs[0])
        m2 = site_to_mutation(sites[0], funcs[0])
        assert m1.id == m2.id

    def test_status_is_pending(self):
        source = "def f(x):\n    return x + 1\n"
        funcs = _parse_funcs(PythonAdapter(), source)
        sites = collect_mutations(funcs[0])
        m = site_to_mutation(sites[0], funcs[0])
        assert m.status == MutationStatus.pending

    def test_location_matches_site(self):
        source = "def f(x):\n    return x + 1\n"
        funcs = _parse_funcs(PythonAdapter(), source)
        sites = collect_mutations(funcs[0])
        for site in sites:
            m = site_to_mutation(site, funcs[0])
            assert m.location.line == site.line
            assert m.location.col_start == site.col_start
            assert m.location.col_end == site.col_end


# ---------------------------------------------------------------------------
# MutagenEngine
# ---------------------------------------------------------------------------


class TestMutagenEngine:
    def test_basic_mutation_generation(self, mutagen: MutagenEngine, tmp_path: Path):
        source = "def add(a, b):\n    return a + b\n"
        (tmp_path / "lib.py").write_text(source)
        adapter = PythonAdapter()
        eigen_result = EigentestEngine(adapter=adapter).run([tmp_path / "lib.py"],
                                                            project_root=tmp_path)
        result = mutagen.run(eigen_result.functions, eigen_result.invariants, AgonConfig())
        assert result.pending_count > 0

    def test_generator_function_skipped_by_engine(self, mutagen: MutagenEngine, tmp_path: Path):
        source = "def gen(n):\n    for i in range(n):\n        yield i\n"
        (tmp_path / "g.py").write_text(source)
        adapter = PythonAdapter()
        eigen = EigentestEngine(adapter=adapter).run([tmp_path / "g.py"], project_root=tmp_path)
        result = mutagen.run(eigen.functions, eigen.invariants, AgonConfig())
        assert result.pending_count == 0
        assert "gen" in result.functions_skipped

    def test_skip_pattern_excludes_test_files(self, mutagen: MutagenEngine, tmp_path: Path):
        source = "def f(x):\n    return x + 1\n"
        (tmp_path / "test_lib.py").write_text(source)
        adapter = PythonAdapter()
        eigen = EigentestEngine(adapter=adapter).run(
            [tmp_path / "test_lib.py"], project_root=tmp_path
        )
        result = mutagen.run(eigen.functions, eigen.invariants, AgonConfig())
        # test_lib.py matches default skip pattern '**/test_*.py'
        assert result.pending_count == 0

    def test_max_mutants_per_function_respected(self, mutagen: MutagenEngine, tmp_path: Path):
        source = textwrap.dedent("""\
            def lots(a, b, c, d):
                if a > 0 and b >= 1 and c < 10 and d <= 5:
                    return a + b - c * d
                return 0
        """)
        (tmp_path / "lots.py").write_text(source)
        adapter = PythonAdapter()
        eigen = EigentestEngine(adapter=adapter).run(
            [tmp_path / "lots.py"], project_root=tmp_path
        )
        cfg = AgonConfig()
        cfg.mutagen.max_mutants_per_function = 3
        result = mutagen.run(eigen.functions, eigen.invariants, cfg)
        assert result.pending_count <= 3

    def test_cross_function_deduplication(self, mutagen: MutagenEngine, tmp_path: Path):
        """Mutations on nested functions should not produce duplicates."""
        source = textwrap.dedent("""\
            def outer(x):
                def inner(y):
                    return y + 1
                return inner(x)
        """)
        (tmp_path / "nested.py").write_text(source)
        adapter = PythonAdapter()
        eigen = EigentestEngine(adapter=adapter).run(
            [tmp_path / "nested.py"], project_root=tmp_path
        )
        result = mutagen.run(eigen.functions, eigen.invariants, AgonConfig())
        ids = [m.id for m in result.mutations]
        assert len(ids) == len(set(ids)), "Duplicate mutation IDs found"

    def test_invariants_linked_to_mutations(self, mutagen: MutagenEngine, tmp_path: Path):
        source = "def cmp(x: int, y: int) -> bool:\n    return x > y\n"
        (tmp_path / "cmp.py").write_text(source)
        adapter = PythonAdapter()
        eigen = EigentestEngine(adapter=adapter).run(
            [tmp_path / "cmp.py"], project_root=tmp_path
        )
        result = mutagen.run(eigen.functions, eigen.invariants, AgonConfig())
        # At least one comparison mutation should be linked to type_constraint invariant
        comp_mutations = [m for m in result.mutations
                          if m.operator == MutationOperator.comparison_boundary]
        assert comp_mutations
        # At least one should have linked invariants (because type annotations exist)
        assert any(m.target_invariants for m in comp_mutations)

    def test_critical_pattern_functions_come_first(self, tmp_path: Path):
        """Functions in auth/ should appear before functions in lib/."""
        (tmp_path / "auth").mkdir()
        (tmp_path / "lib").mkdir()
        (tmp_path / "auth" / "tokens.py").write_text(
            "def verify(token: str) -> bool:\n    return token == 'secret'\n"
        )
        (tmp_path / "lib" / "utils.py").write_text(
            "def add(a, b):\n    return a + b\n"
        )
        adapter = PythonAdapter()
        eigen = EigentestEngine(adapter=adapter).run([tmp_path], project_root=tmp_path)

        cfg = AgonConfig()
        # Default critical_patterns includes **/auth/**
        m_engine = MutagenEngine(adapter=adapter)
        result = m_engine.run(eigen.functions, eigen.invariants, cfg)

        assert result.pending_count > 0
        # The first mutation should come from the auth module
        first_file = result.mutations[0].function_refs[0].file
        assert "auth" in first_file
