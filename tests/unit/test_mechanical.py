"""Tests for mechanical invariant extraction."""
from __future__ import annotations

import pytest
from agon.adapters.python import PythonAdapter
from agon.eigentest import mechanical
from agon.models.schema import InvariantCategory

@pytest.fixture
def adapter() -> PythonAdapter:
    return PythonAdapter()

def test_extract_type_annotations(adapter: PythonAdapter):
    source = """
def process(amount: int, name: str | None) -> bool:
    return True
"""
    tree = adapter.parse(source)
    funcs = adapter.get_functions(tree, "test.py", source)
    assert len(funcs) == 1
    func = funcs[0]

    invs = mechanical.extract(func)
    
    # Expected: 1 for int param, 1 for str|None param, 1 for bool return
    categories = [inv.category for inv in invs]
    assert InvariantCategory.precondition in categories
    assert InvariantCategory.type_constraint in categories
    
    # Check bool return invariant
    bool_inv = next(inv for inv in invs if "bool" in inv.property)
    assert bool_inv.category == InvariantCategory.type_constraint
    assert "isinstance(result, bool)" in bool_inv.property_code

    # Check int param invariant
    int_inv = next(inv for inv in invs if "amount" in inv.property)
    assert int_inv.category == InvariantCategory.precondition
    assert "isinstance(amount, int)" in int_inv.property_code

def test_extract_literal_returns(adapter: PythonAdapter):
    source = """
def check_status(code: int):
    if code == 200:
        return "OK"
    if code == 404:
        return "NOT_FOUND"
    return "UNKNOWN"
"""
    tree = adapter.parse(source)
    funcs = adapter.get_functions(tree, "test.py", source)
    func = funcs[0]

    invs = mechanical.extract(func)
    domain_inv = next(inv for inv in invs if inv.category == InvariantCategory.value_domain)
    assert "OK" in domain_inv.property
    assert "NOT_FOUND" in domain_inv.property
    assert "UNKNOWN" in domain_inv.property
    assert "assert result in" in domain_inv.property_code

def test_extract_asserts(adapter: PythonAdapter):
    source = """
def divide(a: int, b: int) -> float:
    assert b != 0
    result = a / b
    assert result > 0
    return result
"""
    tree = adapter.parse(source)
    funcs = adapter.get_functions(tree, "test.py", source)
    func = funcs[0]

    invs = mechanical.extract(func)
    
    # Precondition assert
    pre_inv = next(inv for inv in invs if "b != 0" in inv.property)
    assert pre_inv.category == InvariantCategory.precondition
    
    # Postcondition assert (contains 'result')
    post_inv = next(inv for inv in invs if "result > 0" in inv.property)
    assert post_inv.category == InvariantCategory.postcondition

def test_extract_exceptions(adapter: PythonAdapter):
    source = """
def withdraw(amount: int):
    if amount < 0:
        raise ValueError("negative")
    if amount > 1000:
        raise RuntimeError
"""
    tree = adapter.parse(source)
    funcs = adapter.get_functions(tree, "test.py", source)
    func = funcs[0]

    invs = mechanical.extract(func)
    exc_invs = [inv for inv in invs if inv.category == InvariantCategory.exception]
    assert len(exc_invs) == 2
    
    val_err = next(inv for inv in exc_invs if "ValueError" in inv.property)
    assert "amount < 0" in val_err.property
    assert "pytest.raises(ValueError)" in val_err.property_code

def test_purity_detection_direct_io(adapter: PythonAdapter):
    source = """
def log_and_return(x):
    print(x)
    return x
"""
    tree = adapter.parse(source)
    funcs = adapter.get_functions(tree, "test.py", source)
    func = funcs[0]

    invs = mechanical.extract(func)
    purity_invs = [inv for inv in invs if inv.category == InvariantCategory.purity]
    # Should NOT have a purity invariant saying it "appears to be pure"
    assert not any("appears to be pure" in inv.property for inv in purity_invs)

def test_purity_detection_pure(adapter: PythonAdapter):
    source = """
def add(a, b):
    return a + b
"""
    tree = adapter.parse(source)
    funcs = adapter.get_functions(tree, "test.py", source)
    func = funcs[0]

    invs = mechanical.extract(func)
    purity_inv = next(inv for inv in invs if inv.category == InvariantCategory.purity)
    assert "appears to be pure" in purity_inv.property

def test_mutable_default_argument(adapter: PythonAdapter):
    source = """
def append_to(val, my_list=[]):
    my_list.append(val)
    return my_list
"""
    tree = adapter.parse(source)
    funcs = adapter.get_functions(tree, "test.py", source)
    func = funcs[0]

    invs = mechanical.extract(func)
    purity_inv = next(inv for inv in invs if "mutable default argument" in inv.property)
    assert purity_inv.category == InvariantCategory.purity
    assert purity_inv.confidence == 0.80

def test_optional_return_type(adapter: PythonAdapter):
    source = """
def find(key: str) -> Optional[str]:
    return None
"""
    tree = adapter.parse(source)
    funcs = adapter.get_functions(tree, "test.py", source)
    invs = mechanical.extract(funcs[0])

    domain_inv = next(inv for inv in invs if inv.category == InvariantCategory.value_domain)
    assert "None" in domain_inv.property
    assert "str" in domain_inv.property
    assert domain_inv.confidence == 0.85


def test_literal_return_type(adapter: PythonAdapter):
    source = '''
def status() -> Literal["ok", "error"]:
    return "ok"
'''
    tree = adapter.parse(source)
    funcs = adapter.get_functions(tree, "test.py", source)
    invs = mechanical.extract(funcs[0])

    domain_inv = next(inv for inv in invs if inv.category == InvariantCategory.value_domain)
    assert '"ok"' in domain_inv.property
    assert '"error"' in domain_inv.property


def test_exception_without_condition(adapter: PythonAdapter):
    """raise at the top of a function body (no enclosing if) has no condition text."""
    source = """
def always_raises():
    raise NotImplementedError
"""
    tree = adapter.parse(source)
    funcs = adapter.get_functions(tree, "test.py", source)
    invs = mechanical.extract(funcs[0])

    exc_inv = next(inv for inv in invs if inv.category == InvariantCategory.exception)
    assert "NotImplementedError" in exc_inv.property
    # No condition clause — property should not contain "when"
    assert "when" not in exc_inv.property


def test_purity_suppressed_by_global(adapter: PythonAdapter):
    source = """
_counter = 0

def increment():
    global _counter
    _counter += 1
"""
    tree = adapter.parse(source)
    funcs = adapter.get_functions(tree, "test.py", source)
    func = next(f for f in funcs if f.ref.name == "increment")
    invs = mechanical.extract(func)

    assert not any(
        "appears to be pure" in inv.property
        for inv in invs
        if inv.category == InvariantCategory.purity
    )


def test_purity_suppressed_by_nonlocal(adapter: PythonAdapter):
    source = """
def outer():
    count = 0
    def inner():
        nonlocal count
        count += 1
    return inner
"""
    tree = adapter.parse(source)
    funcs = adapter.get_functions(tree, "test.py", source)
    inner = next(f for f in funcs if "inner" in f.ref.name)
    invs = mechanical.extract(inner)

    assert not any(
        "appears to be pure" in inv.property
        for inv in invs
        if inv.category == InvariantCategory.purity
    )


def test_purity_transitive_same_file(adapter: PythonAdapter):
    source = """
def internal_impure():
    print("side effect")

def caller():
    internal_impure()
    return 1
"""
    tree = adapter.parse(source)
    funcs = adapter.get_functions(tree, "test.py", source)
    
    internal = next(f for f in funcs if "internal_impure" in f.ref.name)
    caller = next(f for f in funcs if "caller" in f.ref.name)
    
    # 1. Extraction without knowing internal is impure
    invs_init = mechanical.extract(caller)
    assert any("appears to be pure" in inv.property for inv in invs_init if inv.category == InvariantCategory.purity)
    
    # 2. Extraction with known_impure
    invs_trans = mechanical.extract(caller, known_impure=frozenset(["internal_impure"]))
    assert not any("appears to be pure" in inv.property for inv in invs_trans if inv.category == InvariantCategory.purity)


def test_purity_limitation_attribute_mutation(adapter: PythonAdapter):
    """CURRENT LIMITATION: Direct attribute assignment (self.x = 1) is not yet detected as impurity."""
    source = """
class Counter:
    def inc(self):
        self.count += 1
"""
    tree = adapter.parse(source)
    funcs = adapter.get_functions(tree, "test.py", source)
    func = next(f for f in funcs if "inc" in f.ref.name)
    invs = mechanical.extract(func)

    # Currently, this incorrectly passes as 'pure' because no call/global/nonlocal is used
    assert any(
        "appears to be pure" in inv.property
        for inv in invs
        if inv.category == InvariantCategory.purity
    )


def test_type_annotation_limitation_union(adapter: PythonAdapter):
    """CURRENT LIMITATION: Union types using | or Union[] are not yet converted to invariants."""
    source = "def f(x: int | float): pass"
    tree = adapter.parse(source)
    funcs = adapter.get_functions(tree, "test.py", source)
    invs = mechanical.extract(funcs[0])

    # No precondition invariants extracted for 'int | float' yet
    pre_invs = [inv for inv in invs if inv.category == InvariantCategory.precondition]
    assert len(pre_invs) == 0

