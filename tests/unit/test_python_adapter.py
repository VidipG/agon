"""Tests for the Python language adapter."""
from __future__ import annotations

import pytest
from agon.adapters.python import PythonAdapter
from agon.models.schema import InvariantCategory

@pytest.fixture
def adapter() -> PythonAdapter:
    return PythonAdapter()

def test_get_functions_flat(adapter: PythonAdapter):
    source = """
def foo(): pass
def bar(x: int) -> str: return str(x)
"""
    tree = adapter.parse(source)
    funcs = adapter.get_functions(tree, "test.py", source)
    assert len(funcs) == 2
    names = {f.ref.name for f in funcs}
    assert names == {"foo", "bar"}

def test_get_functions_nested(adapter: PythonAdapter):
    source = """
def outer():
    def inner():
        pass
    return inner
"""
    tree = adapter.parse(source)
    funcs = adapter.get_functions(tree, "test.py", source)
    assert len(funcs) == 2
    names = {f.ref.name for f in funcs}
    assert names == {"outer", "outer.inner"}

def test_get_functions_classes(adapter: PythonAdapter):
    source = """
class MyClass:
    def method(self, x):
        return x
    
    @staticmethod
    def static_method():
        pass

def global_func():
    pass
"""
    tree = adapter.parse(source)
    funcs = adapter.get_functions(tree, "test.py", source)
    assert len(funcs) == 3
    names = {f.ref.name for f in funcs}
    assert "MyClass.method" in names
    assert "MyClass.static_method" in names
    assert "global_func" in names
    
    # Check method flag
    method = next(f for f in funcs if f.ref.name == "MyClass.method")
    assert method.is_method is True
    
    global_f = next(f for f in funcs if f.ref.name == "global_func")
    assert global_f.is_method is False

def test_get_functions_async(adapter: PythonAdapter):
    source = """
async def async_func():
    await asyncio.sleep(0)
"""
    tree = adapter.parse(source)
    funcs = adapter.get_functions(tree, "test.py", source)
    assert len(funcs) == 1
    assert funcs[0].is_async is True

def test_extract_docstrings(adapter: PythonAdapter):
    source = """
def documented():
    \"\"\"This is a docstring.\"\"\"
    return True

def undocumented():
    pass
"""
    tree = adapter.parse(source)
    funcs = adapter.get_functions(tree, "test.py", source)
    
    doc = next(f for f in funcs if f.ref.name == "documented")
    assert doc.docstring == "This is a docstring."
    
    undoc = next(f for f in funcs if f.ref.name == "undocumented")
    assert undoc.docstring is None

def test_extract_decorators(adapter: PythonAdapter):
    source = """
@deco1
@deco2(arg=1)
def decorated():
    pass
"""
    tree = adapter.parse(source)
    funcs = adapter.get_functions(tree, "test.py", source)
    assert len(funcs) == 1
    assert funcs[0].decorators == ["deco1", "deco2"]

def test_extract_params_and_annotations(adapter: PythonAdapter):
    source = """
def func(a, b: int, c: str = "default", *args, **kwargs) -> float:
    pass
"""
    tree = adapter.parse(source)
    funcs = adapter.get_functions(tree, "test.py", source)
    func = funcs[0]

    # self/cls are filtered out by _extract_params helper in PythonAdapter
    params = dict(func.params)
    assert "a" in params
    assert params["a"] is None
    assert params["b"] == "int"
    assert params["c"] == "str"

    assert func.return_annotation == "float"


def test_adapter_metadata(adapter: PythonAdapter):
    """Verify the adapter provides correct discovery metadata."""
    assert adapter.source_extensions() == (".py",)
    assert "test_*.py" in adapter.test_file_patterns()
    assert "*_test.py" in adapter.test_file_patterns()


def test_adapter_extract_invariants(adapter: PythonAdapter):
    """Verify that the adapter correctly delegates to the mechanical extractor."""
    source = "def add(a: int, b: int) -> int: return a + b"
    tree = adapter.parse(source)
    func = adapter.get_functions(tree, "test.py", source)[0]
    
    invs = adapter.extract_invariants(func)
    
    # Check that we got invariants (type constraints from annotations)
    categories = {inv.category for inv in invs}
    assert InvariantCategory.precondition in categories
    assert InvariantCategory.type_constraint in categories


def test_adapter_collect_direct_call_names(adapter: PythonAdapter):
    """Verify that the adapter can identify direct calls in a function body."""
    source = """
def main():
    helper()
    self.method()
    obj.save(1, 2)
    print("done")
"""
    tree = adapter.parse(source)
    func = adapter.get_functions(tree, "test.py", source)[0]
    
    names = adapter.collect_direct_call_names(func)
    
    # Should find the leaf names of all calls
    assert "helper" in names
    assert "method" in names
    assert "save" in names
    assert "print" in names


def test_async_method_in_class(adapter: PythonAdapter):
    """Async methods inside classes must be extracted with is_async=True."""
    source = """
class Service:
    async def fetch(self, url: str) -> str:
        return url

    def sync_method(self):
        pass
"""
    tree = adapter.parse(source)
    funcs = adapter.get_functions(tree, "test.py", source)

    fetch = next(f for f in funcs if f.ref.name == "Service.fetch")
    sync = next(f for f in funcs if f.ref.name == "Service.sync_method")

    assert fetch.is_async is True
    assert fetch.is_method is True
    assert sync.is_async is False


def test_decorated_async_function(adapter: PythonAdapter):
    """Decorated async functions must be extracted and flagged as async."""
    source = """
@some_decorator
async def handler(request):
    return request
"""
    tree = adapter.parse(source)
    funcs = adapter.get_functions(tree, "test.py", source)

    assert len(funcs) == 1
    assert funcs[0].ref.name == "handler"
    assert funcs[0].is_async is True
    assert funcs[0].decorators == ["some_decorator"]


def test_unicode_before_function_byte_offsets(adapter: PythonAdapter):
    """Function names must be correct even when non-ASCII characters appear
    before the function in the file (tree-sitter byte offsets != char offsets)."""
    source = "# ─── Unicode box-drawing chars above ───\n\ndef my_function(x: int) -> int:\n    return x\n"
    tree = adapter.parse(source)
    funcs = adapter.get_functions(tree, "test.py", source)

    assert len(funcs) == 1
    assert funcs[0].ref.name == "my_function"
    assert funcs[0].return_annotation == "int"


def test_line_numbers(adapter: PythonAdapter):
    """line_start and line_end must reflect the 1-indexed source line."""
    source = "def first(): pass\n\ndef second():\n    return 1\n"
    tree = adapter.parse(source)
    funcs = adapter.get_functions(tree, "test.py", source)

    first = next(f for f in funcs if f.ref.name == "first")
    second = next(f for f in funcs if f.ref.name == "second")

    assert first.ref.line_start == 1
    assert second.ref.line_start == 3
    assert second.ref.line_end == 4
