"""
LibCST-backed mutation site collector for Python source code.

Design notes
------------
* LibCST's PositionProvider gives exact character positions for operator/literal
  tokens — whitespace-free for binary/comparison/boolean operators and literals.
* We collect MutationSite objects by visiting the full module CST filtered to
  the target function's line range, so we never accidentally mutate decorators,
  docstrings, or code in sibling functions.
* Each site is validated: applying the mutation must produce parseable Python
  (libcst.parse_module check). Invalid mutations are silently dropped.
* Generator functions are skipped entirely (yield/yield from are side-effect
  boundaries that mutation testing cannot safely probe).
* Multi-line expressions for return_value_replace are skipped (single-line only).

CRITICAL: Always pass the full file source to collect_mutations(), not just the
function's extracted source. Positions are file-relative, which matches the
Location stored in Mutation and consumed by PythonAdapter.apply_mutation().
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

import libcst as cst
from libcst.metadata import MetadataWrapper, PositionProvider

from ..models.schema import Location, Mutation, MutationOperator, MutationOperatorClass, MutationStatus
from .base import FunctionNode


# ---------------------------------------------------------------------------
# Public data structure
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MutationSite:
    """A single location in source code that can be mutated.

    ``line`` is 1-indexed; ``col_start`` / ``col_end`` are 0-indexed byte
    columns.  Applying the mutation replaces ``source_line[col_start:col_end]``
    with ``mutated``.
    """

    line: int
    col_start: int
    col_end: int
    original: str   # text being replaced (matches source_line[col_start:col_end])
    mutated: str    # replacement text
    operator: MutationOperator


# ---------------------------------------------------------------------------
# Operator swap tables
# ---------------------------------------------------------------------------

_BOOL_LITERAL_SWAPS: dict[str, str] = {
    "True":  "False",
    "False": "True",
}


# ---------------------------------------------------------------------------
# LibCST visitor
# ---------------------------------------------------------------------------


class _MutationCollector(cst.CSTVisitor):
    """Collect all mutation sites within [func_start, func_end] (inclusive, 1-indexed)."""

    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(self, func_start: int, func_end: int, source_lines: list[str]) -> None:
        self._start = func_start
        self._end = func_end
        self._source_lines = source_lines
        self.sites: list[MutationSite] = []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _in_range(self, node: cst.CSTNode) -> bool:
        pos = self.get_metadata(PositionProvider, node)
        return self._start <= pos.start.line <= self._end

    def _add(
        self,
        node: cst.CSTNode,
        original: str,
        mutated: str,
        operator: MutationOperator,
    ) -> None:
        if not self._in_range(node):
            return
        cr = self.get_metadata(PositionProvider, node)
        col_start = cr.start.column
        col_end = cr.end.column
        if col_start >= col_end:
            return
        self.sites.append(MutationSite(
            line=cr.start.line,
            col_start=col_start,
            col_end=col_end,
            original=original,
            mutated=mutated,
            operator=operator,
        ))

    # ------------------------------------------------------------------
    # Arithmetic operators  (inside BinaryOperation.operator)
    # ------------------------------------------------------------------

    def visit_Add(self, node: cst.Add) -> None:
        self._add(node, "+", "-", MutationOperator.arithmetic_swap)

    def visit_Subtract(self, node: cst.Subtract) -> None:
        self._add(node, "-", "+", MutationOperator.arithmetic_swap)

    def visit_Multiply(self, node: cst.Multiply) -> None:
        self._add(node, "*", "/", MutationOperator.arithmetic_swap)

    def visit_Divide(self, node: cst.Divide) -> None:
        self._add(node, "/", "*", MutationOperator.arithmetic_swap)

    def visit_FloorDivide(self, node: cst.FloorDivide) -> None:
        self._add(node, "//", "/", MutationOperator.arithmetic_swap)

    def visit_Modulo(self, node: cst.Modulo) -> None:
        self._add(node, "%", "+", MutationOperator.arithmetic_swap)

    def visit_Power(self, node: cst.Power) -> None:
        self._add(node, "**", "*", MutationOperator.arithmetic_swap)

    # ------------------------------------------------------------------
    # Comparison operators  (inside ComparisonTarget.operator)
    # ------------------------------------------------------------------

    def visit_GreaterThan(self, node: cst.GreaterThan) -> None:
        self._add(node, ">", ">=", MutationOperator.comparison_boundary)

    def visit_GreaterThanEqual(self, node: cst.GreaterThanEqual) -> None:
        self._add(node, ">=", ">", MutationOperator.comparison_boundary)

    def visit_LessThan(self, node: cst.LessThan) -> None:
        self._add(node, "<", "<=", MutationOperator.comparison_boundary)

    def visit_LessThanEqual(self, node: cst.LessThanEqual) -> None:
        self._add(node, "<=", "<", MutationOperator.comparison_boundary)

    def visit_Equal(self, node: cst.Equal) -> None:
        self._add(node, "==", "!=", MutationOperator.comparison_boundary)

    def visit_NotEqual(self, node: cst.NotEqual) -> None:
        self._add(node, "!=", "==", MutationOperator.comparison_boundary)

    # ------------------------------------------------------------------
    # Boolean operators  (inside BooleanOperation.operator)
    # ------------------------------------------------------------------

    def visit_And(self, node: cst.And) -> None:
        self._add(node, "and", "or", MutationOperator.boolean_negate)

    def visit_Or(self, node: cst.Or) -> None:
        self._add(node, "or", "and", MutationOperator.boolean_negate)

    # ------------------------------------------------------------------
    # Literal / constant mutations
    # ------------------------------------------------------------------

    def visit_Name(self, node: cst.Name) -> None:
        """True ↔ False; None → 0."""
        replacement = _BOOL_LITERAL_SWAPS.get(node.value)
        if replacement is not None:
            self._add(node, node.value, replacement, MutationOperator.constant_replace)
        elif node.value == "None":
            self._add(node, "None", "0", MutationOperator.constant_replace)

    def visit_Integer(self, node: cst.Integer) -> None:
        """Numeric literals: 0 → 1, 1 → 2, n → n-1.

        We avoid mutating to 0 from a positive literal to reduce division-by-zero
        noise: a mutant that crashes with ZeroDivisionError is classified as
        killed anyway, but it obscures the real test signal and produces
        confusing counterexample stubs. Mutating 1 → 2 (rather than 1 → 0)
        keeps the value non-zero while still perturbing the constant.
        """
        try:
            n = int(node.value)
        except ValueError:
            return
        if n == 0:
            replacement = "1"
        elif n == 1:
            replacement = "2"
        else:
            replacement = str(n - 1)
        self._add(node, node.value, replacement, MutationOperator.constant_replace)

    def visit_SimpleString(self, node: cst.SimpleString) -> None:
        """Non-empty string literals → empty string of the same quote style."""
        raw = node.value
        for q in ('"""', "'''", '"', "'"):
            if raw.startswith(q) and raw.endswith(q) and len(raw) > 2 * len(q):
                self._add(node, raw, q + q, MutationOperator.constant_replace)
                break

    # ------------------------------------------------------------------
    # Condition negation  (if / while test expressions)
    # ------------------------------------------------------------------

    def _negate_condition(self, test_node: cst.CSTNode) -> None:
        """Emit a condition_negate site for a single-line test expression."""
        if not self._in_range(test_node):
            return
        pos = self.get_metadata(PositionProvider, test_node)
        if pos.start.line != pos.end.line:
            return  # skip multi-line conditions
        col_start = pos.start.column
        col_end = pos.end.column
        if col_start >= col_end:
            return
        original = _source_slice(self._source_lines, pos.start.line, col_start, col_end)
        if not original:
            return
        self.sites.append(MutationSite(
            line=pos.start.line,
            col_start=col_start,
            col_end=col_end,
            original=original,
            mutated=f"not ({original})",
            operator=MutationOperator.condition_negate,
        ))

    def visit_If(self, node: cst.If) -> None:
        self._negate_condition(node.test)

    def visit_While(self, node: cst.While) -> None:
        # Skip while True / while False — negation produces unreachable bodies
        # or infinite-loop inversions that don't meaningfully test logic.
        if isinstance(node.test, cst.Name) and node.test.value in ("True", "False"):
            return
        self._negate_condition(node.test)

    # ------------------------------------------------------------------
    # Exception swallow  (raise X → pass)
    # ------------------------------------------------------------------

    def visit_Raise(self, node: cst.Raise) -> None:
        """Replace raise with pass, swallowing the exception."""
        if not self._in_range(node):
            return
        pos = self.get_metadata(PositionProvider, node)
        if pos.start.line != pos.end.line:
            return
        col_start = pos.start.column
        col_end = pos.end.column
        if col_start >= col_end:
            return
        original = _source_slice(self._source_lines, pos.start.line, col_start, col_end)
        if not original:
            return
        self.sites.append(MutationSite(
            line=pos.start.line,
            col_start=col_start,
            col_end=col_end,
            original=original,
            mutated="pass",
            operator=MutationOperator.exception_swallow,
        ))

    # ------------------------------------------------------------------
    # Statement deletion  (assignment → pass)
    # ------------------------------------------------------------------

    def _delete_statement(self, node: cst.CSTNode) -> None:
        """Replace a single-line statement with pass."""
        if not self._in_range(node):
            return
        pos = self.get_metadata(PositionProvider, node)
        if pos.start.line != pos.end.line:
            return  # skip multi-line statements
        col_start = pos.start.column
        col_end = pos.end.column
        if col_start >= col_end:
            return
        original = _source_slice(self._source_lines, pos.start.line, col_start, col_end)
        if not original:
            return
        self.sites.append(MutationSite(
            line=pos.start.line,
            col_start=col_start,
            col_end=col_end,
            original=original,
            mutated="pass",
            operator=MutationOperator.statement_delete,
        ))

    def visit_Assign(self, node: cst.Assign) -> None:
        self._delete_statement(node)

    def visit_AugAssign(self, node: cst.AugAssign) -> None:
        self._delete_statement(node)

    def visit_AnnAssign(self, node: cst.AnnAssign) -> None:
        # Only delete annotated assignments that have a value (x: int = expr)
        if node.value is not None:
            self._delete_statement(node)

    # ------------------------------------------------------------------
    # Return value replacement
    # ------------------------------------------------------------------

    def visit_Return(self, node: cst.Return) -> None:
        """Replace non-None single-line return values with None."""
        if node.value is None:
            return
        if isinstance(node.value, cst.Name) and node.value.value == "None":
            return
        if not self._in_range(node.value):
            return

        pos = self.get_metadata(PositionProvider, node.value)
        if pos.start.line != pos.end.line:
            return  # skip multi-line expressions

        line = pos.start.line
        col_start = pos.start.column
        col_end = pos.end.column
        if col_start >= col_end:
            return

        original = _source_slice(self._source_lines, line, col_start, col_end)
        if not original:
            return

        self.sites.append(MutationSite(
            line=line,
            col_start=col_start,
            col_end=col_end,
            original=original,
            mutated="None",
            operator=MutationOperator.return_value_replace,
        ))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def collect_mutations(func: FunctionNode) -> list[MutationSite]:
    """Return all valid mutation sites for *func* (file-relative positions).

    Args:
        func: The function to mutate.  ``func.source`` must be the FULL file
              source so that positions are file-relative.

    Returns:
        Deduplicated, syntax-validated list of MutationSite objects.
    """
    if _is_generator(func):
        return []

    source = func.source
    source_lines = source.splitlines()

    try:
        module = cst.parse_module(source)
    except cst.ParserSyntaxError:
        return []

    wrapper = MetadataWrapper(module)
    collector = _MutationCollector(
        func_start=func.ref.line_start,
        func_end=func.ref.line_end,
        source_lines=source_lines,
    )
    wrapper.visit(collector)

    valid = [s for s in collector.sites if _is_valid_mutation(source, s)]
    return _dedup(valid)


def site_to_mutation(site: MutationSite, func: FunctionNode) -> Mutation:
    """Convert a MutationSite to a Mutation model object (status=pending).

    The mutation ID is deterministic: sha256 of (file, function, operator,
    line, col_start, mutated).  This means the same logical mutation always
    has the same ID across runs, enabling caching and incremental reports.
    """
    key = (
        f"{func.ref.file}:{func.ref.name}:"
        f"{site.operator}:{site.line}:{site.col_start}:{site.mutated}"
    )
    mutation_id = hashlib.sha256(key.encode()).hexdigest()[:16]

    return Mutation(
        id=mutation_id,
        function_refs=[func.ref],
        target_invariants=[],  # linked by MutagenEngine based on invariant categories
        operator=site.operator,
        operator_class=MutationOperatorClass.mechanical,
        original_code=site.original,
        mutated_code=site.mutated,
        location=Location(
            line=site.line,
            col_start=site.col_start,
            col_end=site.col_end,
        ),
        status=MutationStatus.pending,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _apply_site(source: str, site: MutationSite) -> str:
    """Apply a MutationSite to *source* and return the mutated string."""
    lines = source.splitlines(keepends=True)
    if site.line < 1 or site.line > len(lines):
        return source
    row = lines[site.line - 1]
    lines[site.line - 1] = row[: site.col_start] + site.mutated + row[site.col_end :]
    return "".join(lines)


def _is_valid_mutation(source: str, site: MutationSite) -> bool:
    """Return True if applying *site* produces valid Python syntax."""
    mutated = _apply_site(source, site)
    try:
        cst.parse_module(mutated)
        return True
    except cst.ParserSyntaxError:
        return False


def _dedup(sites: list[MutationSite]) -> list[MutationSite]:
    """Remove duplicate sites that would produce the same mutated source."""
    seen: set[tuple[int, int, int, str]] = set()
    result: list[MutationSite] = []
    for s in sites:
        key = (s.line, s.col_start, s.col_end, s.mutated)
        if key not in seen:
            seen.add(key)
            result.append(s)
    return result


def _source_slice(lines: list[str], line: int, col_start: int, col_end: int) -> str:
    """Extract text from a 1-indexed line using 0-indexed columns."""
    if line < 1 or line > len(lines):
        return ""
    return lines[line - 1][col_start:col_end]


def _is_generator(func: FunctionNode) -> bool:
    """Return True if the function body contains yield / yield from."""
    from tree_sitter import Node as _TSNode

    def _walk(node: _TSNode):
        yield node
        for child in node.children:
            yield from _walk(child)

    for node in _walk(func.node):
        if node.type in ("yield", "yield_statement", "yield_expression"):
            return True
    return False
