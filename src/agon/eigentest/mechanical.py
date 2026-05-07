"""
Mechanical invariant extractor.

Deterministic, no model calls. Operates on tree-sitter ASTs. Produces
high-confidence invariants from:
  - Type annotations (confidence 0.85)
  - Explicit assert statements in function body (confidence 0.80)
  - Return value enumeration — if all returns are literals (confidence 0.75)
  - Raise patterns — exception invariants (confidence 0.80)
  - Purity detection — no I/O or global mutation (confidence 0.70)
  - Mutable default arguments — flag purity violation (confidence 0.80)
"""
from __future__ import annotations

from tree_sitter import Node

from ..adapters.base import FunctionNode
from ..models.schema import (
    FunctionRef,
    Invariant,
    InvariantCategory,
    InvariantSource,
)

# Known impure call names (conservative: any uncertainty means no purity claim)
_IO_CALL_NAMES = frozenset({
    "open", "print", "input", "write", "read",
    "get", "post", "put", "delete", "patch",   # httpx/requests methods
    "execute", "fetchone", "fetchall",          # DB cursors
    "send", "recv", "connect", "bind",          # sockets
    "sleep", "system", "popen",
})

# In-place mutation method names — calling these on a mutable object means the
# function mutates its argument, which is an observable side effect.
_MUTATING_METHOD_NAMES = frozenset({
    "append", "extend", "insert", "remove", "pop", "clear",
    "update", "setdefault", "add", "discard",
    "sort", "reverse",
})

_MUTABLE_DEFAULT_TYPES = frozenset({
    "list", "dict", "set",
    "[",    # list literal
    "{",    # dict/set literal
})


def extract(
    func: FunctionNode,
    known_impure: frozenset[str] = frozenset(),
) -> list[Invariant]:
    """Run all mechanical extractors and return the combined invariant list.

    Args:
        known_impure: Names of same-file functions already determined to be
            impure. Passed through to purity detection for transitive checking.
    """
    invariants: list[Invariant] = []

    invariants.extend(_extract_type_annotation_invariants(func))
    invariants.extend(_extract_assert_invariants(func))
    invariants.extend(_extract_return_value_invariants(func))
    invariants.extend(_extract_exception_invariants(func))
    purity_inv = _extract_purity_invariant(func, known_impure=known_impure)
    if purity_inv:
        invariants.append(purity_inv)
    mutable_default = _extract_mutable_default_invariant(func)
    if mutable_default:
        invariants.append(mutable_default)

    return invariants


# ---------------------------------------------------------------------------
# Type annotation invariants
# ---------------------------------------------------------------------------


def _extract_type_annotation_invariants(func: FunctionNode) -> list[Invariant]:
    """Produce invariants from return type and parameter type annotations."""
    invariants: list[Invariant] = []

    if func.return_annotation:
        ann = func.return_annotation
        inv = _invariant_from_return_type(func.ref, ann)
        if inv:
            invariants.append(inv)

    for param_name, ann in func.params:
        if ann:
            inv = _invariant_from_param_type(func.ref, param_name, ann)
            if inv:
                invariants.append(inv)

    return invariants


def _invariant_from_return_type(ref: FunctionRef, ann: str) -> Invariant | None:
    ann = ann.strip()

    # None return — no useful domain invariant
    if ann in ("None", "NoReturn"):
        return None

    # Optional[X] / X | None — result may be None
    if ann.startswith("Optional[") or " | None" in ann or "None |" in ann:
        if ann.startswith("Optional[") and ann.endswith("]"):
            inner = ann[len("Optional["):-1]
        else:
            inner = ann.replace(" | None", "").replace("None | ", "").strip()
        prop = f"returns {inner} or None"
        code = f"assert result is None or isinstance(result, {_annotation_to_type(inner)})"
        return _make_invariant(
            ref=ref,
            category=InvariantCategory.value_domain,
            prop=prop,
            code=code,
            confidence=0.85,
            evidence=[f"Return type annotation: {ann}"],
        )

    # Literal["a", "b"] — explicit value domain
    if ann.startswith("Literal["):
        values_str = ann[len("Literal["):-1]
        prop = f"return value is one of {values_str}"
        code = f"assert result in ({values_str},)"
        return _make_invariant(
            ref=ref,
            category=InvariantCategory.value_domain,
            prop=prop,
            code=code,
            confidence=0.85,
            evidence=[f"Return type annotation: {ann}"],
        )

    # bool — must be True or False
    if ann == "bool":
        return _make_invariant(
            ref=ref,
            category=InvariantCategory.type_constraint,
            prop="returns a bool",
            code="assert isinstance(result, bool)",
            confidence=0.85,
            evidence=["Return type annotation: bool"],
        )

    # int, str, float, bytes — type constraint
    if ann in ("int", "str", "float", "bytes", "list", "dict", "set", "tuple"):
        return _make_invariant(
            ref=ref,
            category=InvariantCategory.type_constraint,
            prop=f"returns {ann}",
            code=f"assert isinstance(result, {ann})",
            confidence=0.85,
            evidence=[f"Return type annotation: {ann}"],
        )

    return None


def _invariant_from_param_type(
    ref: FunctionRef, param_name: str, ann: str
) -> Invariant | None:
    ann = ann.strip()
    if ann in ("int", "str", "float", "bool", "bytes", "list", "dict", "set"):
        return _make_invariant(
            ref=ref,
            category=InvariantCategory.precondition,
            prop=f"parameter '{param_name}' is {ann}",
            code=f"assert isinstance({param_name}, {ann})",
            confidence=0.85,
            evidence=[f"Parameter type annotation: {param_name}: {ann}"],
        )
    return None


def _annotation_to_type(ann: str) -> str:
    """Best-effort: map annotation string to a isinstance-compatible name."""
    mapping = {
        "str": "str", "int": "int", "float": "float",
        "bool": "bool", "bytes": "bytes", "list": "list",
        "dict": "dict", "set": "set", "tuple": "tuple",
    }
    return mapping.get(ann.strip(), ann.strip())


# ---------------------------------------------------------------------------
# Assert invariants
# ---------------------------------------------------------------------------


def _extract_assert_invariants(func: FunctionNode) -> list[Invariant]:
    """Extract assert statements from the function body."""
    invariants: list[Invariant] = []
    body = _child_by_type(func.node, "block")
    if body is None:
        return invariants

    for node in _walk(body):
        if node.type == "assert_statement":
            text = func.source_bytes[node.start_byte: node.end_byte].decode("utf-8", errors="replace")
            # Skip trivially true asserts (assert True, assert 1)
            if text.strip() in ("assert True", "assert 1", "assert 1 == 1"):
                continue
            # Determine if this is a precondition (on a param) or postcondition (on result/return)
            import re as _re
            category = InvariantCategory.precondition
            param_names = {p for p, _ in func.params}
            _RESULT_NAMES = ("result", "output", "ret", "rv")
            if any(_re.search(r"\b" + n + r"\b", text) for n in _RESULT_NAMES):
                category = InvariantCategory.postcondition
            elif not any(_re.search(r"\b" + _re.escape(pname) + r"\b", text) for pname in param_names):
                category = InvariantCategory.postcondition

            invariants.append(_make_invariant(
                ref=func.ref,
                category=category,
                prop=f"assertion holds: {text[len('assert '):].strip()}",
                code=text,
                confidence=0.80,
                evidence=[f"Assert statement at line {node.start_point[0] + 1}"],
            ))
    return invariants


# ---------------------------------------------------------------------------
# Return value enumeration
# ---------------------------------------------------------------------------


def _extract_return_value_invariants(func: FunctionNode) -> list[Invariant]:
    """If all return statements yield literals, infer a value_domain invariant."""
    body = _child_by_type(func.node, "block")
    if body is None:
        return []

    return_values: list[str] = []
    for node in _walk(body):
        if node.type == "return_statement":
            for child in node.children:
                if child.type not in ("return", "comment"):
                    text = func.source_bytes[child.start_byte: child.end_byte].decode("utf-8", errors="replace").strip()
                    if text:
                        return_values.append(text)

    if not return_values:
        return []

    # Check if all are literals (integers, strings, True/False/None)
    all_literal = all(
        _is_literal_node(func, rv) for rv in return_values
    )

    if not all_literal or len(set(return_values)) < 1:
        return []

    values_repr = ", ".join(sorted(set(return_values)))
    return [_make_invariant(
        ref=func.ref,
        category=InvariantCategory.value_domain,
        prop=f"return value is one of: {{{values_repr}}}",
        code=f"assert result in ({values_repr},)",
        confidence=0.75,
        evidence=[f"All return statements yield literals: {values_repr}"],
    )]


def _is_literal_node(func: FunctionNode, text: str) -> bool:
    """Heuristic: is the return value text a Python literal?"""
    text = text.strip()
    if text in ("True", "False", "None"):
        return True
    try:
        int(text)
        return True
    except ValueError:
        pass
    try:
        float(text)
        return True
    except ValueError:
        pass
    if (text.startswith('"') and text.endswith('"')) or (
        text.startswith("'") and text.endswith("'")
    ):
        return True
    return False


# ---------------------------------------------------------------------------
# Exception invariants
# ---------------------------------------------------------------------------


def _extract_exception_invariants(func: FunctionNode) -> list[Invariant]:
    """Extract raise statements and infer exception invariants."""
    body = _child_by_type(func.node, "block")
    if body is None:
        return []

    invariants: list[Invariant] = []
    for node in _walk(body):
        if node.type == "raise_statement":
            text = func.source_bytes[node.start_byte: node.end_byte].decode("utf-8", errors="replace").strip()
            # e.g. "raise ValueError("bad input")" or "raise TypeError"
            exc_name = _parse_raise_type(text)
            if exc_name:
                # Find the enclosing condition if any (look for if parent)
                condition = _find_enclosing_condition(func, node)
                if condition:
                    prop = f"raises {exc_name} when {condition}"
                    code = (
                        f"# When {condition}:\n"
                        f"# with pytest.raises({exc_name}): <call>"
                    )
                else:
                    prop = f"raises {exc_name}"
                    code = f"# with pytest.raises({exc_name}): <call>"

                invariants.append(_make_invariant(
                    ref=func.ref,
                    category=InvariantCategory.exception,
                    prop=prop,
                    code=code,
                    confidence=0.80,
                    evidence=[f"raise statement at line {node.start_point[0] + 1}: {text[:60]}"],
                ))
    return invariants


def _parse_raise_type(text: str) -> str | None:
    """Extract the exception class name from a raise statement.

    Handles:
      - ``raise ValueError(...)``           → "ValueError"
      - ``raise module.MyException(...)``   → "MyException"
      - ``raise ValueError``                → "ValueError"
      - ``raise`` (bare re-raise)           → None
      - ``raise e`` (variable)              → None (cannot determine type statically)
    """
    text = text.removeprefix("raise").strip()
    if not text:
        return None
    # Strip call arguments: "ValueError('msg')" → "ValueError"
    name_part = text.split("(")[0].strip()
    # Take the last segment of a dotted path: "module.MyException" → "MyException"
    name = name_part.split(".")[-1].strip()
    if not name or not name.isidentifier():
        return None
    # Heuristic: exception classes are PascalCase or end with 'Error'/'Warning'/'Exception'.
    # A single lowercase name without those suffixes is almost certainly a variable re-raise.
    if name[0].islower() and not any(
        name.endswith(suffix) for suffix in ("Error", "Warning", "Exception", "Exit")
    ):
        return None
    return name


def _find_enclosing_condition(func: FunctionNode, raise_node: Node) -> str | None:
    """Walk up from the raise node to find the nearest if condition text."""
    # We need to check the parent chain — tree-sitter nodes have parent references
    node = raise_node.parent
    while node is not None:
        if node.type == "if_statement":
            condition_node = None
            for child in node.children:
                if child.type not in ("if", ":", "block", "comment"):
                    condition_node = child
                    break
            if condition_node:
                return func.source_bytes[condition_node.start_byte: condition_node.end_byte].decode("utf-8", errors="replace").strip()
        node = node.parent
    return None


# ---------------------------------------------------------------------------
# Purity detection
# ---------------------------------------------------------------------------


def _extract_purity_invariant(
    func: FunctionNode,
    known_impure: frozenset[str] = frozenset(),
) -> Invariant | None:
    """If no I/O or global mutation is detected, infer purity.

    Args:
        known_impure: Names of functions in the same file already determined
            to be impure. If this function calls any of them it is also impure.
            This enables same-file transitive purity without a full call graph.
            Cross-module calls are not traced — see evidence string.
    """
    body = _child_by_type(func.node, "block")
    if body is None:
        return None

    if _has_mutable_default(func):
        return None

    impure_callee: str | None = None

    for node in _walk(body):
        if node.type == "call":
            call_text = func.source_bytes[node.start_byte: node.end_byte].decode("utf-8", errors="replace")
            func_part = call_text.split("(")[0].strip()
            name_parts = func_part.split(".")
            leaf = name_parts[-1]
            # Direct I/O name match
            if any(part in _IO_CALL_NAMES for part in name_parts):
                return None
            # Mutating method call on an object (e.g. lst.append(x))
            if len(name_parts) > 1 and leaf in _MUTATING_METHOD_NAMES:
                return None
            # Same-file transitive: leaf name is a known-impure function
            if leaf in known_impure:
                impure_callee = leaf

        if node.type == "global_statement":
            return None

        if node.type == "nonlocal_statement":
            return None

    if impure_callee is not None:
        return None

    return _make_invariant(
        ref=func.ref,
        category=InvariantCategory.purity,
        prop="function appears to be pure (no I/O or global mutation detected)",
        code="# Purity: no assert; verified by mutation and counterexample analysis",
        confidence=0.70,
        evidence=[
            "No I/O calls, global, or nonlocal statements detected in function body. "
            "Same-file callees checked transitively; cross-module calls not traced."
        ],
    )


# ---------------------------------------------------------------------------
# Mutable default argument
# ---------------------------------------------------------------------------


def _extract_mutable_default_invariant(func: FunctionNode) -> Invariant | None:
    """Detect mutable default arguments and flag them as purity violations."""
    params_node = _child_by_type(func.node, "parameters")
    if params_node is None:
        return None

    for child in params_node.children:
        if child.type in ("default_parameter", "typed_default_parameter"):
            # Find the default value node (after "=")
            found_eq = False
            for sub in child.children:
                if sub.type == "=":
                    found_eq = True
                elif found_eq and sub.type in ("list", "dictionary", "set"):
                    text = func.source_bytes[sub.start_byte: sub.end_byte].decode("utf-8", errors="replace")
                    return _make_invariant(
                        ref=func.ref,
                        category=InvariantCategory.purity,
                        prop=f"mutable default argument detected: {text[:40]}",
                        code=(
                            "# WARNING: mutable default argument — state persists across calls.\n"
                            "# Use None as default and create inside function body."
                        ),
                        confidence=0.80,
                        evidence=[f"Mutable default argument: {text[:60]}"],
                    )
    return None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_invariant(
    ref: FunctionRef,
    category: InvariantCategory,
    prop: str,
    code: str,
    confidence: float,
    evidence: list[str],
) -> Invariant:
    inv_id = Invariant.compute_id([ref], prop)
    return Invariant(
        id=inv_id,
        function_refs=[ref],
        category=category,
        property=prop,
        property_code=code,
        confidence=confidence,
        source=InvariantSource.mechanical,
        evidence=evidence,
    )


def _child_by_type(node: Node, type_: str) -> Node | None:
    for child in node.children:
        if child.type == type_:
            return child
    return None


def _walk(node: Node):
    """Pre-order walk of all descendants."""
    yield node
    for child in node.children:
        yield from _walk(child)


def _has_mutable_default(func: FunctionNode) -> bool:
    params_node = _child_by_type(func.node, "parameters")
    if params_node is None:
        return False
    for child in params_node.children:
        if child.type in ("default_parameter", "typed_default_parameter"):
            found_eq = False
            for sub in child.children:
                if sub.type == "=":
                    found_eq = True
                elif found_eq and sub.type in ("list", "dictionary", "set"):
                    return True
    return False
