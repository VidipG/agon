"""Tests for the eigentest engine and transitive purity pass."""
from __future__ import annotations

from pathlib import Path
import pytest
from agon.adapters.python import PythonAdapter
from agon.eigentest.engine import EigentestEngine
from agon.models.schema import InvariantCategory

@pytest.fixture
def engine() -> EigentestEngine:
    return EigentestEngine(adapter=PythonAdapter())

def test_engine_transitive_purity_chain(engine: EigentestEngine, tmp_path: Path):
    """Test A -> B -> C(impure) correctly marks A and B as impure."""
    source = """
def top_level():
    return mid_level()

def mid_level():
    return leaf_impure()

def leaf_impure():
    print("impure")
    return 42

def independent_pure():
    return 100
"""
    py_file = tmp_path / "transitive.py"
    py_file.write_text(source)

    result = engine.run(paths=[py_file], project_root=tmp_path)
    
    # leaf_impure is impure (direct)
    # mid_level is impure (calls leaf_impure)
    # top_level is impure (calls mid_level)
    # independent_pure is pure
    
    # Get the purity status for all functions found
    all_function_names = [f.ref.name for f in result.functions]
    purity_map = {name: False for name in all_function_names}
    
    for name, invs in result.invariants_by_function.items():
        if any(inv.category == InvariantCategory.purity and "appears to be pure" in inv.property 
               for inv in invs):
            purity_map[name] = True
    
    assert purity_map["leaf_impure"] is False
    assert purity_map["mid_level"] is False
    assert purity_map["top_level"] is False
    assert purity_map["independent_pure"] is True

def test_engine_function_filtering(engine: EigentestEngine, tmp_path: Path):
    source = """
def foo(): return 1
def bar(): return 2
"""
    py_file = tmp_path / "filter.py"
    py_file.write_text(source)

    # Filter by name
    result = engine.run(paths=[py_file], functions_filter=["foo"], project_root=tmp_path)
    assert len(result.functions) == 1
    assert result.functions[0].ref.name == "foo"

def test_engine_directory_scanning(engine: EigentestEngine, tmp_path: Path):
    """Engine must recurse into a directory and analyze all .py files."""
    (tmp_path / "a.py").write_text("def func_a(): return 1\n")
    (tmp_path / "b.py").write_text("def func_b(): return 2\n")
    (tmp_path / "not_python.txt").write_text("ignored\n")

    result = engine.run(paths=[tmp_path], project_root=tmp_path)

    names = {f.ref.name for f in result.functions}
    assert "func_a" in names
    assert "func_b" in names
    assert len(result.functions) == 2


def test_engine_function_ref_relative_to_project_root(engine: EigentestEngine, tmp_path: Path):
    """FunctionRef.file paths must be relative to project_root, not absolute."""
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "module.py").write_text("def my_func(): pass\n")

    result = engine.run(paths=[sub], project_root=tmp_path)

    assert len(result.functions) == 1
    # Path should be relative to tmp_path, not absolute
    assert result.functions[0].ref.file == "pkg/module.py"


def test_engine_impurity_does_not_cross_files(engine: EigentestEngine, tmp_path: Path):
    """An impure function in file_a must not taint a pure function in file_b
    even if it shares the same leaf name."""
    (tmp_path / "file_a.py").write_text(
        "def helper():\n    print('side effect')\n    return 1\n"
    )
    (tmp_path / "file_b.py").write_text(
        # Same leaf name 'helper' — but this one is pure
        "def helper():\n    return 42\n"
    )

    result = engine.run(paths=[tmp_path], project_root=tmp_path)

    # file_b's helper is pure — file_a's impurity must not bleed over
    file_b_helper = next(
        f for f in result.functions
        if f.ref.file == "file_b.py" and f.ref.name == "helper"
    )
    invs = result.invariants_by_function["helper"]
    file_b_invs = [i for i in invs if i.function_refs[0].file == "file_b.py"]
    assert any(
        "appears to be pure" in inv.property
        for inv in file_b_invs
        if inv.category == InvariantCategory.purity
    )


def test_engine_recursion_termination(engine: EigentestEngine, tmp_path: Path):
    """Ensure the transitive pass terminates on mutual recursion."""
    source = """
def func_a():
    return func_b()

def func_b():
    return func_a()
"""
    py_file = tmp_path / "recursion.py"
    py_file.write_text(source)

    # Should terminate and both should be pure (no I/O detected)
    result = engine.run(paths=[py_file], project_root=tmp_path)
    assert len(result.functions) == 2
    for name in ["func_a", "func_b"]:
        invs = result.invariants_by_function[name]
        assert any(inv.category == InvariantCategory.purity and "appears to be pure" in inv.property 
                  for inv in invs)
