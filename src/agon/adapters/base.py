"""
LanguageAdapter protocol and shared data structures.

Nothing outside src/agon/adapters/ should import tree-sitter, libCST, or any
language-specific package directly. This is the boundary that enables
multi-language support without rewriting eigentest, mutagen, or spectre.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from tree_sitter import Node, Tree

from ..models.schema import FunctionRef, Mutation


@dataclass
class FunctionNode:
    """A parsed function, ready for invariant extraction and mutation.

    Wraps the tree-sitter node with pre-extracted metadata so callers do not
    need to know tree-sitter node types.
    """

    ref: FunctionRef
    node: Node                        # tree-sitter node (function_definition)
    source: str                       # full source text (str, for display)
    source_bytes: bytes               # UTF-8 bytes; use for tree-sitter offset slicing
    is_async: bool = False
    is_method: bool = False
    decorators: list[str] = field(default_factory=list)
    docstring: str | None = None
    # Parameters: list of (name, annotation_text | None)
    params: list[tuple[str, str | None]] = field(default_factory=list)
    return_annotation: str | None = None


@dataclass
class TypeInfo:
    params: list[tuple[str, str | None]]  # (name, type_annotation | None)
    return_type: str | None


@dataclass
class TestAssertion:
    """A single assertion extracted from a test function."""

    test_file: str
    test_name: str
    target_function: str        # name of the function under test
    assertion_code: str         # raw assertion source text
    line: int


@dataclass
class TestResult:
    passed: bool
    stdout: str
    stderr: str
    duration_ms: int
    killed_mutant: bool = False      # True if at least one test failed
    timed_out: bool = False          # True if the test runner exceeded the timeout
    error_message: str | None = None # Non-None when status should be MutationStatus.error


class LanguageAdapter(Protocol):
    """Single point of language-specific logic in Agon.

    Both eigentest and mutagen consume a LanguageAdapter. Neither imports
    tree-sitter, libCST, or any language-specific package directly.
    """

    def parse(self, source: str) -> Tree:
        """Parse source text into a tree-sitter Tree."""
        ...

    def get_functions(self, tree: Tree, file_path: str, source: str) -> list[FunctionNode]:
        """Extract all function definitions from a parsed tree.

        Recurses into nested functions. Names use dot-qualified form:
          module.ClassName.method_name
          module.outer_func.inner_func
        """
        ...

    def apply_mutation(self, source: str, mutation: Mutation) -> str:
        """Apply a single mutation to source text, returning the mutated source."""
        ...

    def get_type_info(self, func: FunctionNode) -> TypeInfo:
        """Return parameter types and return type for a function."""
        ...

    def extract_test_assertions(
        self, test_source: str, test_file: str
    ) -> list[TestAssertion]:
        """Parse test assertions from a test file source."""
        ...

    def run_tests(
        self,
        project_root: Path,
        test_filter: list[str] | None = None,
        timeout_seconds: float = 120,
        extra_env: dict[str, str] | None = None,
    ) -> TestResult:
        """Run the test suite (or a subset) and return the result.

        Args:
            project_root: Directory to invoke the test runner from.
            test_filter: If provided, restrict to these test paths/IDs.
            timeout_seconds: Kill the runner after this many seconds.
            extra_env: Additional environment variables merged into os.environ.
        """
        ...
