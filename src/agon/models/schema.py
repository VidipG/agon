"""
Core data model for Agon's inter-stage communication.

All pipeline components (eigentest, mutagen, spectre) exchange data using
these types. The schema is versioned to support forward compatibility.

Schema version: 1.0
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class InvariantCategory(StrEnum):
    value_domain = "value_domain"       # return/param ∈ {set} or [range]
    relational = "relational"           # input-output relationship (monotonicity, idempotency)
    precondition = "precondition"       # required state/values before call
    postcondition = "postcondition"     # guaranteed state/values after call
    exception = "exception"             # conditions under which exceptions are raised
    type_constraint = "type_constraint" # narrower than the declared type
    purity = "purity"                   # no side effects, deterministic


class InvariantSource(StrEnum):
    mechanical = "mechanical"               # AST/type analysis, no model calls
    llm_inferred = "llm_inferred"           # LLM semantic inference
    spec_derived = "spec_derived"           # extracted from an external spec document
    runtime_observed = "runtime_observed"   # Daikon/DynaPyt dynamic analysis
    human_defined = "human_defined"         # written by a developer


class MutationOperatorClass(StrEnum):
    mechanical = "mechanical"   # Tier 1: fixed set, no LLM
    llm_guided = "llm_guided"   # Tier 2: require semantic context


class MutationStatus(StrEnum):
    pending = "pending"
    killed = "killed"
    survived = "survived"
    equivalent = "equivalent"
    timeout = "timeout"
    error = "error"


class Severity(StrEnum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"


# ---------------------------------------------------------------------------
# Core types
# ---------------------------------------------------------------------------


class FunctionRef(BaseModel):
    """Identity reference to a specific function in the codebase.

    Primary key: (file, name). Survives line shifts but not renames.
    content_hash detects body changes across runs — a changed hash
    invalidates cached invariants for this function.

    line_start / line_end are display hints only, never used for identity.
    For nested functions, name uses dot-qualified form:
      module.outer_func.inner_func
    """

    file: str           # relative path from project root
    name: str           # qualified: module.Class.method or module.outer.inner
    line_start: int     # display only — re-derived at parse time
    line_end: int       # display only
    signature: str      # e.g. "(amount: int, currency: str) -> bool"
    content_hash: str   # sha256 of function body

    @classmethod
    def compute_hash(cls, body: str) -> str:
        return hashlib.sha256(body.encode()).hexdigest()


class Invariant(BaseModel):
    """A behavioral property that should always hold for a function.

    property      — human-readable, language-agnostic description
    property_code — executable assertion; used by mutagen and spectre

    These two fields serve different audiences and can diverge. When they do,
    it signals that the executable check is an approximation of the stated
    property — which is worth surfacing in the report.
    """

    id: str                                         # sha256(function_refs + property)
    function_refs: list[FunctionRef]                # v1: len == 1; vN: cross-function
    category: InvariantCategory
    property: str                                   # human-readable, language-agnostic
    property_code: str                              # executable assertion
    confidence: float = Field(ge=0.0, le=1.0)
    source: InvariantSource
    evidence: list[str] = Field(default_factory=list)   # provenance strings
    contradiction: bool = False                     # set when sources directly conflict
    subsumed_by: str | None = None                  # id of narrower invariant that subsumes this
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    @classmethod
    def compute_id(cls, function_refs: list[FunctionRef], property_text: str) -> str:
        key = "|".join(f"{r.file}:{r.name}" for r in function_refs) + "|" + property_text
        return hashlib.sha256(key.encode()).hexdigest()[:16]


class MutationOperator(StrEnum):
    # Tier 1 — mechanical (fixed set, no LLM, exhaustively enumerable)
    comparison_boundary = "comparison_boundary"     # >= → >, < → <=
    boolean_negate = "boolean_negate"               # and → or, not x → x
    arithmetic_swap = "arithmetic_swap"             # + → -, * → /
    constant_replace = "constant_replace"           # 0 → 1, True → False
    return_value_replace = "return_value_replace"   # return x → return None
    statement_delete = "statement_delete"           # remove a line
    exception_swallow = "exception_swallow"         # raise X → pass
    condition_negate = "condition_negate"           # if x: → if not x:

    # Tier 2 — LLM-guided (require semantic context; extensible)
    boundary_shift = "boundary_shift"               # off-by-one in loop/comparison bounds
    guard_removal = "guard_removal"                 # delete an edge case check
    default_mutation = "default_mutation"           # change a default parameter value
    type_coercion = "type_coercion"                 # change type casting behavior
    semantic_swap = "semantic_swap"                 # replace with semantically related but wrong value


class Location(BaseModel):
    line: int
    col_start: int
    col_end: int


class Mutation(BaseModel):
    """A single behavioral change applied to a function for testing purposes.

    original_code / mutated_code contain the literal source text of the
    affected expression or statement (1-3 lines) — not the whole function.
    Together they form the reviewable diff that developers inspect.
    """

    id: str
    function_refs: list[FunctionRef]        # v1: len == 1
    target_invariants: list[str]            # invariant IDs this mutation is designed to violate
    operator: MutationOperator
    operator_class: MutationOperatorClass
    original_code: str                      # source text of the mutated span (1-3 lines)
    mutated_code: str                       # replacement source text
    location: Location
    status: MutationStatus = MutationStatus.pending
    killing_tests: list[str] = Field(default_factory=list)
    execution_time_ms: int | None = None


class Counterexample(BaseModel):
    """A concrete input demonstrating a behavioral gap in the test suite.

    oracle_agreement: tri-state
      true  — both oracles agree on expected behavior
      false — oracles disagree; flag for human review (one may be wrong)
      None  — no spec provided; only the invariant oracle was evaluated
    """

    id: str
    mutation_id: str
    invariant_id: str
    input: Any                              # the generated input
    expected: Any                           # what the invariant says should happen
    actual: Any                             # what the original code produces
    mutant_output: Any                      # what the mutant produces
    oracle_agreement: bool | None           # true / false / None (no spec)
    reproducer_code: str                    # standalone executable test
    severity: Severity


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


class ReportSummary(BaseModel):
    functions_analyzed: int = 0
    invariants_inferred: int = 0
    invariants_by_source: dict[str, int] = Field(default_factory=dict)
    mutations_generated: int = 0
    mutations_killed: int = 0
    mutations_survived: int = 0
    mutations_equivalent: int = 0
    mutation_score: float = 0.0         # killed / (total - equivalent)
    counterexamples_found: int = 0
    iteration_count: int = 0


class AgonReport(BaseModel):
    """Top-level output of the Agon pipeline."""

    schema_version: str = SCHEMA_VERSION
    project: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    scope: list[str]                    # files/functions analyzed
    invariants: list[Invariant] = Field(default_factory=list)
    mutations: list[Mutation] = Field(default_factory=list)
    counterexamples: list[Counterexample] = Field(default_factory=list)
    summary: ReportSummary = Field(default_factory=ReportSummary)
