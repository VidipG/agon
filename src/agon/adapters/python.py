"""
PythonAdapter — tree-sitter based language adapter for Python.

Responsibilities:
  - Parse Python source into tree-sitter trees
  - Extract function definitions (including nested, async, class methods)
  - Extract type information from annotations
  - Run pytest and collect results

IMPORTANT: tree-sitter byte offsets refer to positions in the UTF-8 encoded
bytes, not Python str character positions. All node text extraction must use
source_bytes (bytes), never source (str), for slicing.

Mutation application (libCST) is added in the mutagen phase.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import tree_sitter_python as _tsp
from tree_sitter import Language, Node, Parser, Tree

from ..models.schema import FunctionRef, Mutation
from .base import FunctionNode, LanguageAdapter, TestAssertion, TestResult, TypeInfo

_PY_LANGUAGE = Language(_tsp.language())
_PARSER = Parser(_PY_LANGUAGE)

_IO_CALL_NAMES = frozenset({
    "open", "print", "input", "read", "write", "readline", "readlines",
    "requests", "httpx", "aiohttp", "urllib",
    "os", "subprocess", "socket", "sleep", "system",
})


# ---------------------------------------------------------------------------
# Public adapter
# ---------------------------------------------------------------------------


class PythonAdapter:
    """LanguageAdapter implementation for Python (tree-sitter + pytest)."""

    def source_extensions(self) -> tuple[str, ...]:
        return (".py",)

    def test_file_patterns(self) -> tuple[str, ...]:
        return ("test_*.py", "*_test.py")

    def parse(self, source: str) -> Tree:
        return _PARSER.parse(source.encode("utf-8"))

    def get_functions(self, tree: Tree, file_path: str, source: str) -> list[FunctionNode]:
        """Extract all function definitions, recursing into classes and nested defs."""
        src_bytes = source.encode("utf-8")
        results: list[FunctionNode] = []
        _walk_functions(
            node=tree.root_node,
            source=source,
            src_bytes=src_bytes,
            file_path=file_path,
            scope_prefix="",
            results=results,
            is_method=False,
        )
        return results

    def extract_invariants(
        self, func: FunctionNode, known_impure: frozenset[str] = frozenset()
    ) -> list[Invariant]:
        from ..eigentest import mechanical
        return mechanical.extract(func, known_impure=known_impure)

    def apply_mutation(self, source: str, mutation: Mutation) -> str:
        loc = mutation.location
        lines = source.splitlines(keepends=True)
        line = lines[loc.line - 1]
        mutated_line = line[: loc.col_start] + mutation.mutated_code + line[loc.col_end :]
        lines[loc.line - 1] = mutated_line
        return "".join(lines)

    def get_type_info(self, func: FunctionNode) -> TypeInfo:
        return TypeInfo(
            params=func.params,
            return_type=func.return_annotation,
        )

    def extract_test_assertions(
        self, test_source: str, test_file: str
    ) -> list[TestAssertion]:
        tree = self.parse(test_source)
        src_bytes = test_source.encode("utf-8")
        results: list[TestAssertion] = []
        _collect_test_assertions(tree.root_node, test_source, src_bytes, test_file, results)
        return results

    def run_tests(
        self,
        project_root: Path,
        test_filter: list[str] | None = None,
        timeout_seconds: float = 120,
        extra_env: dict[str, str] | None = None,
    ) -> TestResult:
        import os
        import time

        cmd = ["pytest", "--tb=short", "-q"]
        if test_filter:
            cmd.extend(test_filter)

        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)

        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=env,
            )
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return TestResult(
                passed=proc.returncode == 0,
                stdout=proc.stdout,
                stderr=proc.stderr,
                duration_ms=elapsed_ms,
                killed_mutant=proc.returncode != 0,
                timed_out=False,
            )
        except subprocess.TimeoutExpired:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return TestResult(
                passed=False,
                stdout="",
                stderr=f"Test runner timed out after {timeout_seconds:.0f}s",
                duration_ms=elapsed_ms,
                timed_out=True,
            )

    def collect_direct_call_names(self, func: FunctionNode) -> set[str]:
        """Return the set of bare call names made directly in this function body.

        Only the unqualified leaf name is returned (e.g. `helper` from
        `self.helper(x)`, `log` from `logger.log()`). This is used by the
        engine's transitive purity pass.
        """
        body = _child_by_type(func.node, "block")
        if body is None:
            return set()
        names: set[str] = set()
        _collect_calls_shallow(body, func.source_bytes, names)
        return names


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _bslice(src_bytes: bytes, node: Node) -> str:
    """Extract UTF-8 text from a tree-sitter node using byte offsets."""
    return src_bytes[node.start_byte: node.end_byte].decode("utf-8", errors="replace")


def _walk_functions(
    node: Node,
    source: str,
    src_bytes: bytes,
    file_path: str,
    scope_prefix: str,
    results: list[FunctionNode],
    is_method: bool,
) -> None:
    """Recursively walk tree-sitter nodes, extracting function_definition nodes."""
    for child in node.children:
        if child.type in ("function_definition", "async_function_definition", "decorated_definition"):
            fn_node = child if child.type != "decorated_definition" else _unwrap_decorated(child)
            if fn_node is None:
                continue

            func_node = _build_function_node(
                fn_node=fn_node,
                decorated_node=child if child.type == "decorated_definition" else None,
                source=source,
                src_bytes=src_bytes,
                file_path=file_path,
                scope_prefix=scope_prefix,
                is_method=is_method,
            )
            if func_node is not None:
                results.append(func_node)
                # Recurse into function body for nested defs
                body = _child_by_type(fn_node, "block")
                if body:
                    _walk_functions(
                        node=body,
                        source=source,
                        src_bytes=src_bytes,
                        file_path=file_path,
                        scope_prefix=func_node.ref.name,
                        results=results,
                        is_method=False,
                    )

        elif child.type == "class_definition":
            class_name = _node_text_by_type(child, src_bytes, "identifier") or ""
            new_prefix = f"{scope_prefix}.{class_name}" if scope_prefix else class_name
            body = _child_by_type(child, "block")
            if body:
                _walk_functions(
                    node=body,
                    source=source,
                    src_bytes=src_bytes,
                    file_path=file_path,
                    scope_prefix=new_prefix,
                    results=results,
                    is_method=True,
                )
        else:
            _walk_functions(
                node=child,
                source=source,
                src_bytes=src_bytes,
                file_path=file_path,
                scope_prefix=scope_prefix,
                results=results,
                is_method=is_method,
            )


def _unwrap_decorated(node: Node) -> Node | None:
    """Return the inner function_definition from a decorated_definition node."""
    for child in node.children:
        if child.type in ("function_definition", "async_function_definition"):
            return child
    return None


def _build_function_node(
    fn_node: Node,
    decorated_node: Node | None,
    source: str,
    src_bytes: bytes,
    file_path: str,
    scope_prefix: str,
    is_method: bool,
) -> FunctionNode | None:
    is_async = any(child.type == "async" for child in fn_node.children)

    name_node = _child_by_type(fn_node, "identifier")
    if name_node is None:
        return None
    func_name = _bslice(src_bytes, name_node)
    qualified_name = f"{scope_prefix}.{func_name}" if scope_prefix else func_name

    params = _extract_params(fn_node, src_bytes)
    return_annotation = _extract_return_type(fn_node, src_bytes)

    body_text = _get_body_text(fn_node, src_bytes)
    content_hash = hashlib.sha256(body_text.encode("utf-8")).hexdigest()

    start_line = fn_node.start_point[0] + 1  # 0-indexed → 1-indexed
    end_line = fn_node.end_point[0] + 1

    sig = _build_signature(params, return_annotation)

    ref = FunctionRef(
        file=file_path,
        name=qualified_name,
        line_start=start_line,
        line_end=end_line,
        signature=sig,
        content_hash=content_hash,
    )

    decorators: list[str] = []
    if decorated_node:
        decorators = _extract_decorator_names(decorated_node, src_bytes)

    docstring = _extract_docstring(fn_node, src_bytes)

    return FunctionNode(
        ref=ref,
        node=fn_node,
        source=source,
        source_bytes=src_bytes,
        is_async=is_async,
        is_method=is_method,
        decorators=decorators,
        docstring=docstring,
        params=params,
        return_annotation=return_annotation,
    )


def _extract_params(fn_node: Node, src_bytes: bytes) -> list[tuple[str, str | None]]:
    params_node = _child_by_type(fn_node, "parameters")
    if params_node is None:
        return []

    result: list[tuple[str, str | None]] = []
    for child in params_node.children:
        if child.type == "identifier":
            name = _bslice(src_bytes, child)
            if name not in ("self", "cls"):
                result.append((name, None))
        elif child.type in ("typed_parameter", "typed_default_parameter"):
            name = None
            annotation = None
            for sub in child.children:
                if sub.type == "identifier" and name is None:
                    n = _bslice(src_bytes, sub)
                    if n not in ("self", "cls"):
                        name = n
                elif sub.type == "type":
                    annotation = _bslice(src_bytes, sub)
            if name:
                result.append((name, annotation))
        elif child.type == "default_parameter":
            for sub in child.children:
                if sub.type == "identifier":
                    name = _bslice(src_bytes, sub)
                    if name not in ("self", "cls"):
                        result.append((name, None))
                    break
    return result


def _extract_return_type(fn_node: Node, src_bytes: bytes) -> str | None:
    found_arrow = False
    for child in fn_node.children:
        if child.type == "->":
            found_arrow = True
        elif found_arrow and child.type == "type":
            return _bslice(src_bytes, child)
    return None


def _extract_docstring(fn_node: Node, src_bytes: bytes) -> str | None:
    body = _child_by_type(fn_node, "block")
    if body is None:
        return None
    for child in body.children:
        if child.type == "expression_statement":
            for sub in child.children:
                if sub.type == "string":
                    raw = _bslice(src_bytes, sub)
                    # Strip quotes
                    for q in ('"""', "'''", '"', "'"):
                        if raw.startswith(q) and raw.endswith(q):
                            return raw[len(q): -len(q)].strip()
                    return raw.strip()
    return None


def _extract_decorator_names(decorated_node: Node, src_bytes: bytes) -> list[str]:
    names: list[str] = []
    for child in decorated_node.children:
        if child.type == "decorator":
            text = _bslice(src_bytes, child)
            names.append(text.lstrip("@").split("(")[0].strip())
    return names


def _get_body_text(fn_node: Node, src_bytes: bytes) -> str:
    body = _child_by_type(fn_node, "block")
    if body:
        return _bslice(src_bytes, body)
    return _bslice(src_bytes, fn_node)


def _build_signature(
    params: list[tuple[str, str | None]], return_annotation: str | None
) -> str:
    parts = []
    for name, ann in params:
        parts.append(f"{name}: {ann}" if ann else name)
    sig = "(" + ", ".join(parts) + ")"
    if return_annotation:
        sig += f" -> {return_annotation}"
    return sig


def _child_by_type(node: Node, type_: str) -> Node | None:
    for child in node.children:
        if child.type == type_:
            return child
    return None


def _node_text_by_type(parent: Node, src_bytes: bytes, child_type: str) -> str | None:
    child = _child_by_type(parent, child_type)
    if child:
        return _bslice(src_bytes, child)
    return None


def _collect_test_assertions(
    node: Node,
    source: str,
    src_bytes: bytes,
    test_file: str,
    results: list[TestAssertion],
    current_test: str = "",
) -> None:
    """Walk test file AST collecting assert statements inside test_ functions."""
    for child in node.children:
        if child.type == "function_definition":
            name_node = _child_by_type(child, "identifier")
            if name_node:
                fname = _bslice(src_bytes, name_node)
                if fname.startswith("test_"):
                    body = _child_by_type(child, "block")
                    if body:
                        _collect_test_assertions(body, source, src_bytes, test_file, results, fname)
                    continue
        elif child.type == "assert_statement" and current_test:
            text = _bslice(src_bytes, child)
            results.append(TestAssertion(
                test_file=test_file,
                test_name=current_test,
                target_function="",
                assertion_code=text,
                line=child.start_point[0] + 1,
            ))
        else:
            _collect_test_assertions(child, source, src_bytes, test_file, results, current_test)


def _collect_calls_shallow(node: Node, src_bytes: bytes, names: set[str]) -> None:
    """Walk node collecting call names, but do not recurse into nested defs."""
    for child in node.children:
        if child.type in ("function_definition", "async_function_definition"):
            # Calls inside nested functions belong to them, not the outer one
            continue
        if child.type == "call":
            call_text = src_bytes[child.start_byte: child.end_byte].decode("utf-8", errors="replace")
            leaf = call_text.split("(")[0].rsplit(".", 1)[-1].strip()
            if leaf.isidentifier():
                names.add(leaf)
        _collect_calls_shallow(child, src_bytes, names)
