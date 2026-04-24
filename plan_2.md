# Agon — Comprehensive Technical Plan

## Preamble

This document supersedes `docs/plan.md` and incorporates findings from all prior analyses. It is the implementation spec for Agon's v1 development — each component specifies its interfaces, internal mechanics, testing strategy, and extension points. Sections 1 and 12 provide strategic framing; all other sections are directed at implementers.

### Conventions

- **v1** — The first usable release, scoped to Python.
- **vN** — A future version beyond v1 scope.
- Code examples are illustrative, not final API.

### Terminology

| Term | Definition |
|------|-----------|
| Mechanical extraction | Deterministic analysis requiring no model calls (AST parsing, type reading, pattern matching) |
| Local model (SLM) | A small (1-8B parameter) model running locally via Ollama or compatible server |
| Frontier model | A large cloud-hosted model (Claude, GPT-4o, Gemini) used for reasoning tasks |
| Invariant | A behavioral property of a function that should always hold |
| Oracle | A mechanism for determining whether a function's output is correct for a given input |
| Mutation score | Proportion of non-equivalent mutations detected by the test suite |

### v1 Scope Boundary

| In Scope | Deferred to vN |
|----------|---------------|
| Python single-language | TypeScript, Go, Rust, Java, C# adapters |
| Single-function invariants | Cross-function and temporal invariants |
| Synchronous and async functions | Stateful API sequence testing |
| Mechanical + LLM + spec + Daikon invariant sources | Jira/Linear, protobuf, GraphQL spec parsing |
| Tier 1 + Tier 2 mutation operators | Higher-order, cross-function mutation |
| Process sandbox (default), container sandbox | Cloud sandbox, namespace sandbox |
| LanceDB embedded cache | Qdrant server, cross-project transfer |
| SQLite token tracking | OpenTelemetry export |
| GitHub Actions CI | GitLab, Jenkins, Buildkite |
| CLI + JSON/SARIF output | Web dashboard, IDE plugins |

---

## 1. Problem Statement

AI-assisted code generation is producing a measurable gap between what developers believe their systems do and what those systems actually do. Three forces drive this:

1. **Cognitive surrender.** Developers passively trust LLM-generated code without empirical verification. When the same model produces both code and tests, shared conceptual blind spots mean bugs and the tests that should catch them fail in correlated ways.

2. **Spec-implementation drift.** Spec-driven development adds abstraction between developer intent and generated code. Without systematic verification, the implementation diverges from the specification silently.

3. **The oracle problem.** A test catches a bug only if it reaches the buggy code *and* has an oracle that recognizes incorrect behavior. LLM-generated tests are fitted to LLM-generated code — they assert the code doesn't crash, not that it behaves correctly.

Agon is a tool that addresses all three by constructing an independent verification pipeline: infer what correct behavior should be, measure whether tests enforce it, and find concrete inputs that prove where enforcement fails. The design safeguards against the most dangerous failure mode — codifying bugs as properties — through multi-source inference and confidence scoring (Section 4.2).

---

## 2. Architecture Overview

Agon is a three-stage pipeline with a shared data model. Each stage is independently useful and produces structured output that feeds the next.

```
  ┌─────────────────────────────────────────────────────────┐
  │                    Specification Input                   │
  │          (optional: OpenAPI, markdown, docstrings)       │
  └──────────────────────┬──────────────────────────────────┘
                         │
                         ▼
  ┌──────────────────────────────────────────────────────────┐
  │                     eigentest                            │
  │  Invariant inference from code, specs, and annotations   │
  │                                                          │
  │  Signal sources:                                         │
  │    Static: types, assertions, AST patterns               │
  │    NLP: docstrings, spec documents                       │
  │    LLM: semantic inference from code structure           │
  │                                                          │
  │  Output: Invariant[]  (with confidence scores)           │
  └──────────────────────┬───────────────────────────────────┘
                         │
                         ▼
  ┌──────────────────────────────────────────────────────────┐
  │                      mutagen                             │
  │  Invariant-guided mutation testing                       │
  │                                                          │
  │  Mechanical operators + LLM-guided site selection        │
  │  Impact analysis for test selection                      │
  │  Sandboxed execution                                     │
  │                                                          │
  │  Output: Mutation[] (killed | survived | equivalent)     │
  └──────────────────────┬───────────────────────────────────┘
                         │
                         ▼
  ┌──────────────────────────────────────────────────────────┐
  │                      spectre                             │
  │  Counterexample generation for surviving mutations       │
  │                                                          │
  │  Type-driven + LLM-augmented input generation            │
  │  Dual-oracle validation                                  │
  │                                                          │
  │  Output: Counterexample[]                                │
  └──────────────────────┬───────────────────────────────────┘
                         │
                         ▼
  ┌──────────────────────────────────────────────────────────┐
  │                   Feedback Loop                          │
  │  Spectre findings refine eigentest invariants            │
  │  Confidence scores updated across iterations             │
  │  Terminates on: score threshold | diminishing returns    │
  │                  | budget limit  | max iterations        │
  └──────────────────────────────────────────────────────────┘
```

### Design Principles

1. **Each stage is independently deployable.** A user can run `agon mutagen` without eigentest by supplying hand-written invariants or using mutagen's built-in mechanical operators alone.
2. **Structured intermediate representation.** All inter-stage communication uses a defined schema (Section 3). New tools that produce or consume this schema can plug into any point in the pipeline.
3. **Model diversity by default.** The model that infers invariants must not be the same model that generated the code under test. Different pipeline stages should use different models to avoid correlated blind spots.
4. **Mechanical first, LLM second.** Every stage extracts what it can deterministically before invoking an LLM. The LLM adds signal on top of a mechanical baseline — it is never the sole source of truth.
5. **Incremental by default.** Full-codebase analysis is the exception. The default mode is diff-aware, analyzing only changed functions and their dependents.

---

## 3. Core Data Model

All inter-stage data uses these schemas. Serialization format is JSON. The schemas are versioned to support forward compatibility.

```
schema_version: "1.0"

─────────────────────────────────────────────────────

FunctionRef {
  file:         string     # relative path from project root
  name:         string     # qualified: module.Class.method or module.outer.inner
  line_start:   int        # display only — re-derived at parse time
  line_end:     int        # display only
  signature:    string     # "(amount: int, currency: str) -> bool"
  content_hash: string     # sha256 of function body
}

─────────────────────────────────────────────────────

Invariant {
  id:             string         # deterministic: sha256(function_refs + property)
  function_refs:  FunctionRef[]  # v1: len == 1; vN: cross-function
  category:       enum           # see taxonomy below
  property:       string         # human-readable, language-agnostic
  property_code:  string         # executable, language-specific assertion
  confidence:     float          # 0.0 – 1.0
  source:         enum           # mechanical | llm_inferred | spec_derived | runtime_observed | human_defined
  evidence:       string[]       # provenance strings from each extractor
  created_at:     timestamp
  updated_at:     timestamp
}

Invariant.category enum:
  - value_domain       # return value ∈ {set}, parameter ∈ [range], non-null
  - relational         # input-output relationship (monotonicity, idempotency)
  - precondition       # required state/values before call
  - postcondition      # guaranteed state/values after call
  - exception          # conditions under which exceptions are raised
  - type_constraint    # narrower than the declared type
  - purity             # no side effects, deterministic

─────────────────────────────────────────────────────

Mutation {
  id:                 string
  function_refs:      FunctionRef[]  # v1 constraint: len == 1. vN: cross-function mutation
  target_invariants:  string[]       # invariant IDs this mutation is designed to violate
  operator:           MutationOperator  # predefined enum — see below
  operator_class:     enum           # "mechanical" | "llm_guided"
  original_code:      string         # source text of the mutated span (1-3 lines)
  mutated_code:       string         # replacement source text
  location:           {line: int, col_start: int, col_end: int}
  status:             enum           # "pending" | "killed" | "survived" | "equivalent" | "timeout" | "error"
  killing_tests:      string[]      # test identifiers that killed this mutant
  execution_time_ms:  int?
}

MutationOperator enum:
  # Tier 1 — mechanical (fixed set, no LLM)
  - comparison_boundary    # >= → >, < → <=, etc.
  - boolean_negate         # and → or, not x → x
  - arithmetic_swap        # + → -, * → /
  - constant_replace       # 0 → 1, True → False
  - return_value_replace   # return x → return None
  - statement_delete       # remove a line
  - exception_swallow      # raise X → pass
  - condition_negate       # if x → if not x
  # Tier 2 — LLM-guided (extensible, registered at runtime)
  - boundary_shift         # off-by-one in loop/comparison bounds
  - guard_removal          # delete an edge case check
  - default_mutation       # change a default parameter value
  - type_coercion          # change type casting behavior
  - semantic_swap          # replace value with semantically related but wrong value
  # Custom operators registered via adapter plugins follow the naming
  # convention "custom_<name>" and are tracked in per-operator statistics.

─────────────────────────────────────────────────────

Counterexample {
  id:                  string
  mutation_id:         string
  invariant_id:        string
  input:               json          # the generated input
  expected:            json          # what the invariant says should happen
  actual:              json          # what the original code produces
  mutant_output:       json          # what the mutant produces
  oracle_agreement:    bool?         # true: agree, false: disagree, null: no spec available
  reproducer_code:     string        # standalone executable test
  severity:            enum          # "critical" | "high" | "medium" | "low"
}

─────────────────────────────────────────────────────

AgonReport {
  schema_version:     string
  project:            string
  timestamp:          timestamp
  scope:              string[]       # files/functions analyzed
  invariants:         Invariant[]
  mutations:          Mutation[]
  counterexamples:    Counterexample[]
  summary: {
    functions_analyzed:     int
    invariants_inferred:    int
    invariants_by_source:   map[source -> int]
    mutations_generated:    int
    mutations_killed:       int
    mutations_survived:     int
    mutations_equivalent:   int
    mutation_score:         float      # killed / (total - equivalent)
    counterexamples_found:  int
    iteration_count:        int
  }
}
```

### Design Notes

**FunctionRef identity:** The primary identity key is `(file, name)`, which survives line shifts but not renames. `content_hash` (sha256 of the function body) detects body changes across runs — if the hash differs, the function was modified and cached invariants are invalidated. `line_start` and `line_end` are ephemeral display hints, never used for identity. For nested functions and closures, `name` uses dot-qualified form (e.g., `module.outer_func.inner_func`). In vN, semantic cache embeddings will bridge renames by matching on structural similarity rather than name.

**`property` vs `property_code`:** These fields serve different audiences and can diverge. `property` is a human-readable, language-agnostic description for developer review (e.g., "returns status code in {200, 400, 500}"). `property_code` is the executable assertion used by mutagen and spectre (e.g., `assert result in (200, 400, 500)`). `property` describes intent; `property_code` encodes the mechanism. When they diverge, it signals that the executable check is an approximation of the stated property.

**`evidence`:** Human-readable provenance strings generated by each extractor. Examples: "Type annotation: return type is int", "Test test_payment.py:42 asserts result == 200", "LLM inference: guard clause at line 10 implies precondition", "OpenAPI spec: endpoint returns 200, 400, or 500". When invariants merge during deduplication, evidence lists concatenate to preserve full provenance.

**`function_refs` cross-function (vN):** In v1, each invariant targets a single function. In vN, `function_refs` supports multiple functions for cross-function invariants such as encode/decode pair consistency and call-sequence ordering constraints.

**`original_code` / `mutated_code` scope:** These fields contain the literal source text of the affected expression or statement — not the whole function. Together they form the reviewable diff that developers inspect.

**`oracle_agreement`:** Captures whether the dual oracles (invariant-derived and spec-derived) agree on the expected behavior. `true` = both agree. `false` = they disagree, flagging for human review (one oracle may be wrong). `null` = no spec provided, only the invariant oracle was evaluated. The spec oracle comes from eigentest's spec extractor (Section 4.7): invariants with `source: "spec_derived"`.

### Extensibility

New pipeline stages plug in by producing or consuming these types. A metamorphic testing stage would produce `Invariant` objects with `category: "relational"`. A concolic execution stage would consume `Mutation` objects and produce `Counterexample` objects. The schema is the API contract.

### Future: Schema Evolution

Add fields with defaults. Never remove fields in minor versions. The `schema_version` field enables readers to handle older formats. v2 will likely add `StateSequence` for temporal invariants and `CoverageMap` for per-invariant coverage data.

---

## 4. Component: eigentest — Invariant Inference

### 4.1 Purpose

Infer behavioral properties (invariants) of functions from all available signal. These invariants define what "correct" means for each function, providing the oracle that the rest of the pipeline measures against.

### 4.2 The Circularity Problem and How It Is Addressed

All three prior analyses flag the same fundamental risk: if eigentest infers invariants solely from buggy code, it codifies the bug as a property.

Concrete example:
```python
def process_payment(amount: int) -> int:
    if amount < 0:
        return 500  # Bug: should return 400 for invalid input
    return 200
```

An LLM analyzing this code might infer: *"returns 500 for negative amounts."* This is a false invariant describing current behavior, not correct behavior.

**Mitigation architecture — multi-source inference with confidence tiers:**

eigentest does not treat all signal sources as equal. It extracts invariants from multiple sources and assigns confidence based on the source's independence from the code:

| Source | Extraction Method | Confidence Floor | Independence |
|--------|-------------------|------------------|-------------|
| Type annotations | Mechanical (AST) | 0.85 | High — type system is independent |
| Explicit assertions in code | Mechanical (AST) | 0.80 | High — developer stated intent |
| Existing test assertions | Mechanical (test parser) | 0.60 | Medium — may share blind spots |
| Docstrings | NLP extraction | 0.55 | Medium — often written with code |
| External spec (OpenAPI, markdown) | LLM-structured extraction | 0.70 | High — spec predates implementation |
| LLM inference from code | LLM semantic analysis | 0.30 | Low — may share model blind spots |

Invariants from independent sources (specs, types) anchor the analysis. LLM-inferred invariants are hypotheses that require empirical validation by mutagen before they earn higher confidence.

When external specs are available, eigentest cross-references code-derived invariants against spec-derived invariants and flags divergences. This directly addresses the original thesis: measuring drift between specification and implementation.

### 4.3 Invariant Taxonomy

eigentest categorizes every invariant it produces. The category determines which mutation operators mutagen will apply and what kind of inputs spectre will generate.

**value_domain** — The function's outputs (or inputs) are constrained to specific values or ranges.
- `result ∈ {200, 400, 500}`
- `0 <= result <= 100`
- `result is not None`

**relational** — A relationship between inputs and outputs holds.
- Monotonicity: `f(a) <= f(b) when a <= b`
- Idempotency: `f(f(x)) == f(x)`
- Commutativity: `f(a, b) == f(b, a)`

**precondition** — Required state or parameter constraints for correct execution.
- `amount > 0`
- `token is not expired`

**postcondition** — Guaranteed state after execution.
- `balance == old_balance - amount`
- `len(result) <= len(input)`

**exception** — Conditions under which specific exceptions must be raised.
- `raises ValueError when amount < 0`
- `raises AuthError when token is expired`
- Exception type matching is exact by default: `raises ValueError` means `ValueError` specifically, not `TypeError` or a bare `Exception`. A mutation changing `raise ValueError` to `raise TypeError` should be detected even if tests use broad `except Exception` handlers. The invariant's `property_code` asserts the specific exception type.

**type_constraint** — Narrower than the declared type.
- Declared `str`, but always a valid email address
- Declared `int`, but always positive

**purity** — No side effects, deterministic output for same input.
- The function is pure (no I/O, no mutation of external state)
- The function is idempotent at the API level

### 4.4 Internal Architecture

```
            ┌────────────────────────────────┐
            │        Source Code Input        │
            └───────────────┬────────────────┘
                            │
            ┌───────────────▼────────────────┐
            │    Mechanical Extractor         │
            │    (tree-sitter AST analysis)   │
            │                                 │
            │  - Type annotations             │
            │  - Assert statements            │
            │  - Raise/except patterns        │
            │  - Return value enumeration     │
            │  - Test assertion parsing        │
            └───────────────┬────────────────┘
                            │ Invariant[] (high confidence)
                            │
            ┌───────────────▼────────────────┐
            │    Semantic Cache Lookup        │
            │    (embedding similarity)       │
            │                                 │
            │  Hit → merge cached invariants  │
            │  Miss → continue to LLM chain  │
            └───────────────┬────────────────┘
                            │
          ┌─────────────────┴──────────────────┐
          │ (if spec provided)                 │
          ▼                                    ▼
  ┌───────────────────┐            ┌───────────────────────┐
  │  Spec Extractor   │            │  LLM Inference Chain  │
  │  (LLM-structured  │            │                       │
  │   extraction from  │            │  Stage 1: Local model │
  │   OpenAPI/markdown)│            │   pattern extraction  │
  └────────┬──────────┘            │                       │
           │                       │  Stage 2: Frontier    │
           │                       │   model validation    │
           │                       └───────────┬───────────┘
           │                                   │
           └──────────────┬────────────────────┘
                          │ Invariant[] (mixed confidence)
                          ▼
            ┌─────────────────────────────────┐
            │    Merge & Deduplicate          │
            │                                 │
            │  - Union all invariant sources  │
            │  - Deduplicate by semantic      │
            │    equivalence                  │
            │  - Highest confidence wins      │
            │  - Flag spec↔code divergences   │
            │  - Store in semantic cache      │
            └───────────────┬─────────────────┘
                            │
                            ▼
                      Invariant[]
```

**Merge semantics:** When multiple sources produce invariants for the same function, the merge step unifies them:
- **Deduplication:** Invariants with semantically equivalent `property_code` are merged. The higher-confidence source wins, and evidence lists concatenate.
- **Contradictions:** When two sources produce invariants that directly conflict (e.g., type annotation says `-> int` but docstring says "returns a string"), both are kept with a `contradiction` flag. The report surfaces these for developer review — a contradiction between a high-confidence source (type annotation, 0.85) and a lower source (docstring, 0.55) is a strong signal that the docstring is stale or the type annotation is wrong.
- **Subsumption:** A narrow invariant subsumes a broad one. If one source says `result > 0` and another says `result in {1, 2, 3}`, only the narrower invariant is kept (the broader is marked as subsumed, retained for evidence).

### 4.5 Mechanical Extractor — Implementation Detail

The mechanical extractor operates on tree-sitter ASTs and requires no model calls. It is deterministic and fast.

**Language support for mechanical extraction:**

tree-sitter provides the uniform AST interface. The per-language work is mapping tree-sitter node types to the invariant taxonomy. Available packages:

| Language | tree-sitter Grammar | AST Manipulation | Test Runner | Maturity |
|----------|-------------------|-----------------|-------------|----------|
| Python | tree-sitter-python | libCST | pytest, unittest | High — v1 target |
| TypeScript | tree-sitter-typescript | ts-morph | Jest, Vitest | High |
| Go | tree-sitter-go | go/ast (stdlib) | go test | High |
| Rust | tree-sitter-rust | syn | cargo test | High |
| Java | tree-sitter-java | JavaParser | JUnit | High |
| C# | tree-sitter-c-sharp | Roslyn | xUnit, NUnit | Medium |

**Test assertion parsing per language:**

Test assertion patterns are framework-specific and require per-language pattern matchers. However, tree-sitter makes this manageable — you write tree-sitter S-expression queries against a consistent AST, not different parsers from scratch. Examples of what each language needs:

- **Python:** `assert x == y`, `pytest.raises(X)`, `self.assertEqual(x, y)` — ~15 patterns covers 95%
- **TypeScript:** `expect(x).toBe(y)`, `expect(x).toThrow()` — ~10 patterns for Jest/Vitest
- **Go:** `if got != want { t.Errorf(...) }` — no standard assert library, pattern-match the idiom
- **Rust:** `assert_eq!(x, y)`, `#[should_panic]` — ~8 patterns
- **Java:** `assertEquals(x, y)`, `assertThrows()` — ~12 patterns for JUnit

**Type annotation extraction:**
- Parse function signature for parameter types and return type.
- Narrow: `Optional[X]` implies a `value_domain` invariant (result can be None or X).
- `Literal["a", "b"]` directly maps to a value domain.
- Union types enumerate the domain.
- TypedDict fields imply structural postconditions on return values.

**Assert statement extraction:**
- Scan function body for `assert` statements.
- Parse the condition as an invariant.
- `assert x > 0` → precondition invariant on parameter `x`.
- `assert isinstance(result, int)` → type constraint on return value.

**Return value enumeration:**
- Trace all return paths through the function AST.
- If all return values are literals, infer a value domain invariant.
- If return values include `raise`, map to exception invariants.

**Test assertion extraction:**
- Identify tests that call the target function (import tracing + name matching).
- Parse assertions: `assert f(x) == y`, `pytest.raises(ValueError)`, `assertEqual`.
- Convert each assertion into an invariant with source `"mechanical"`.

**Purity detection:**
- Scan for I/O operations: file handles, network calls, database queries, global mutation.
- If none found, infer `purity` invariant.
- Conservative: any uncertainty means no purity claim.

### 4.6 LLM Inference Chain

**Stage 1 — Local model pattern extraction:**

A small code model (1-8B parameters, running locally) receives:
- The function source code
- Its type annotations (if any)
- Its docstring (if any)
- Its call graph neighbors (callers and callees, 1 hop)
- The mechanically-extracted invariants (as context, not to duplicate)

Task: generate candidate invariants that the mechanical extractor could not derive. Focus on relational invariants, non-obvious preconditions, and business logic constraints.

Output: candidate invariants with explanatory reasoning.

**Stage 2 — Frontier model validation:**

A frontier model receives:
- The function source code
- The Stage 1 candidates
- The mechanical invariants (as ground truth anchors)

Task: for each candidate, classify as:
- **Genuine invariant** — the property is inherent to the function's purpose
- **Accidental correlation** — the property happens to hold but isn't part of the contract
- **False** — the property doesn't hold

Only genuine invariants are kept. Accidental correlations are flagged as low-confidence hypotheses.

**Model diversity enforcement:** The frontier model used in Stage 2 must be from a different model family than the model that generated the code under analysis (if known). This is configurable. The default pairing is a local open-source model for Stage 1 and Claude for Stage 2, which provides diversity against GPT-generated code and vice versa.

**Context window limits:** When a function's source code plus its context (type annotations, docstring, call graph neighbors, mechanical invariants) exceeds the local model's context window, the LLM chain truncates: call graph neighbors are dropped first (farthest hops removed), then the function body is truncated to the first N lines with a `[truncated]` marker. If the function body alone exceeds the context limit, LLM inference is skipped entirely and the function relies on mechanical extraction only. The frontier model in Stage 2 has a larger context window and receives the full function body; only Stage 1 context may be truncated.

### 4.7 Spec Input Channel

eigentest accepts an optional specification input. Supported formats for v1:

- **Markdown** — free-form requirements documents. LLM extracts structured claims.
- **OpenAPI / Swagger** — mechanical extraction of endpoint contracts (status codes, request/response schemas, required fields).
- **Docstrings** — already parsed by the mechanical extractor, but the LLM chain re-examines them for semantic claims the mechanical pass cannot extract.

The spec extractor produces invariants with `source: "spec_derived"` and `confidence: 0.70`. When a spec-derived invariant contradicts a code-derived invariant, eigentest emits a **divergence report** — this is the spec-implementation drift detection that closes the loop on the original thesis.

**vN:** Consume protobuf/gRPC definitions, GraphQL schemas, Jira/Linear ticket descriptions, and inline contract annotations (`icontract`, `deal`).

### 4.8 Semantic Cache

**Purpose:** Avoid redundant LLM inference for structurally similar functions. A function that looks like a CRUD handler should inherit invariant patterns from other CRUD handlers already analyzed.

**Architecture:**

```
Cache key:   embedding(AST_skeleton ⊕ type_signature ⊕ docstring)
Cache value: { invariants: Invariant[], model_version: string, timestamp: timestamp }

AST_skeleton: the function's AST with all identifiers normalized to
              positional placeholders (arg_0, var_1, etc.) and all
              literals replaced with type tokens. This captures
              structure, not naming.
```

- **Embedding model:** A code-specialized embedding model (Voyage Code 3, or jina-embeddings-v3 with code task). The embedding captures structural and semantic similarity.
- **Vector store:** LanceDB for v1 (embedded, zero-infrastructure, local-first). Qdrant for multi-user/server deployments.
- **Similarity threshold:** Configurable, default 0.88. Below this, fall through to LLM inference.
- **Invalidation:** When a function's `content_hash` changes, its cache entry is invalidated. Transitive invalidation: if a callee's signature changes, the caller's cache entry is also invalidated.
- **Confidence adjustment:** Cached invariants are assigned `min(original_confidence, 0.65)` — the cache introduces uncertainty, so confidence is capped below the original until re-validated by mutagen.

**vN:** Cache warming across projects. A library of invariant patterns for common frameworks (Django views, FastAPI endpoints, Flask routes) can be distributed as a pre-built cache.

### 4.9 Bootstrap / Cold Start

When eigentest encounters a function with no type annotations, no docstrings, no tests, and no specs, the signal available is minimal. This is the bootstrap problem — Agon is most needed where it has the least to work with.

**Mitigation:**

1. **Mechanical extraction still works.** Return value enumeration, purity detection, and basic AST patterns produce invariants even without annotations.
2. **LLM inference is most valuable here.** The LLM examines variable names, control flow structure, import context, and call graph position to hypothesize invariants. These start at low confidence (0.25-0.35) but any signal is better than none.
3. **Baseline generation mode.** `agon bootstrap <path>` runs eigentest and then generates a minimal test file that asserts the inferred invariants. This gives the developer a starting point and gives mutagen something to run against on subsequent invocations.

### 4.10 Prioritization

Not every function in a codebase warrants the same analysis depth. eigentest assigns a **priority score** to each function to guide where the pipeline spends its compute budget:

| Factor | Weight | Signal |
|--------|--------|--------|
| Public API surface | High | Exported from module, part of `__all__`, has docstring |
| Security-sensitive | Critical | Name/imports suggest auth, crypto, input validation |
| High fan-in | High | Many callers (dependency graph analysis) |
| Cyclomatic complexity | Medium | Complex control flow = more invariant opportunities |
| Recently modified | Medium | `git log` recency — new code is riskier |
| Low test coverage | Medium | Coverage data (if available) |
| Generated/vendored | Skip | Path heuristics, `# auto-generated` markers |

**Non-pure function handling:** When eigentest's purity detector determines a function has side effects (I/O, state mutation, network calls), the function is not skipped — skipping impure functions would make Agon unusable on real codebases where the majority of functions are impure. Instead:
- Invariant categories `purity` and `relational` (which require deterministic behavior) are suppressed.
- Categories `value_domain`, `precondition`, `postcondition`, `exception`, and `type_constraint` are still inferred — these hold regardless of side effects.
- mutagen applies mutations but the sandbox executes with stricter isolation (no network, no filesystem writes outside temp).
- spectre uses type-driven generation; LLM-augmented generation is limited to input construction without execution-dependent strategies.
- The function's impurity is noted in the report so the developer knows which invariants may be environment-dependent.

### 4.11 Testing eigentest

**Unit tests:**
- Mechanical extractor on known Python functions → verify correct invariants extracted.
- Each invariant category has a reference function with known properties.
- Assert that type annotations always produce `confidence >= 0.85`.

**Integration tests:**
- Run eigentest on a curated corpus of functions with documented correct invariants. Measure precision (% of inferred invariants that are actually correct) and recall (% of known invariants that eigentest finds).
- Run eigentest on functions with known bugs. Verify that code-only invariants have low confidence and that spec-derived invariants (when spec is provided) flag the divergence.

**Regression tests:**
- Snapshot test: store eigentest output for a reference codebase. On model or code changes, diff against snapshot and flag unexpected changes.

**Benchmark:**
- Track inference latency and API cost per function.
- Track cache hit rate over time.

### 4.12 Daikon Integration (Phase 3)

Daikon is elevated to Phase 3 (not future scope) because it is the highest-value signal source for the bootstrap scenario after mechanical extraction.

**Rationale:** When *any* test suite exists (even minimal), Daikon observes runtime behavior and produces invariants at confidence ~0.65 — higher than LLM-inferred (0.30). For codebases with no type annotations or docstrings, runtime-observed invariants are the only empirically grounded signal available.

**Implementation:**
- Instrument test runs to collect execution traces (function entry/exit, parameter values, return values).
- Feed traces to a Daikon-compatible invariant detector. For Python, evaluate DynaPyt and pynguin as alternatives to Java-centric Daikon.
- Produce invariants with `source: "runtime_observed"` and `confidence: 0.65`.
- Compare runtime-observed invariants against LLM-inferred invariants. Divergences are high-value signals: either the LLM hallucinated or the runtime behavior doesn't match expectations.

**Constraint:** Daikon only observes executed paths. If the test suite doesn't exercise a code path, Daikon produces nothing for it. This is complementary to LLM inference (which covers unexecuted paths at lower confidence).

### 4.13 Future Additions

1. **Coverage-aware inference.** Use `coverage.py` data to identify which code paths are actually exercised. Weight invariants by whether they cover tested or untested paths.
2. **Temporal invariants.** Extend the taxonomy to include ordering constraints: "login must precede update_profile." Requires analyzing call sequences, not just individual functions.
3. **Contract annotation output.** Export confirmed high-confidence invariants as `icontract` or `deal` decorators that can be added to the source code as runtime assertions.
4. **Cross-project invariant transfer.** If project A's `authenticate()` has confirmed invariants, and project B has a structurally similar `authenticate()`, transfer the invariants as low-confidence hypotheses via the semantic cache.

### 4.14 Python-Specific Edge Cases

The PythonAdapter must handle several language features that affect invariant extraction, mutation, and counterexample generation across all pipeline stages.

**Async functions.** `async def` functions are prevalent in modern Python (FastAPI, aiohttp, async database clients). They require special handling at every stage:
- **Extraction:** The return type of `async def` is `Coroutine[Any, Any, T]`, not `T`. The mechanical extractor must unwrap coroutine types to extract the inner return type for invariant inference.
- **Mutation execution:** Tests for async code require `pytest-asyncio` or `anyio`. The test runner detection in `LanguageAdapter.run_tests()` must identify async test entry points and configure the appropriate plugin.
- **Counterexample generation:** Spectre's reproducer code must emit `async def test_...` with `await` calls rather than synchronous function calls. The executor must run async reproducers via `asyncio.run()` or equivalent.

**Generator functions.** Functions using `yield` return iterators, not scalar values. Their behavior is spread across multiple `__next__()` calls, and `yield from` delegates to sub-generators. v1 strategy: detect generators during function discovery (presence of `yield` in AST) and apply only `value_domain` invariants to the yielded elements, not return-type invariants. Cross-call invariants (ordering, exhaustion) are deferred to vN.

**Closures and nested functions.** Inner functions defined inside outer functions may share names across different enclosing scopes. The `FunctionRef.name` field uses dot-qualified form (`module.outer_func.inner_func`) to disambiguate. The PythonAdapter's `get_functions()` must recurse into nested `def` statements and construct qualified names from the lexical scope chain.

**Decorator-wrapped functions.** Decorators like `@lru_cache`, `@app.route`, or `@login_required` can replace the original function with a wrapper. v1 strategy: analyze the unwrapped function body (the source code inside the `def`), not the post-decoration callable. The decorator list is recorded as metadata but does not alter invariant inference. Known decorators that change return types (e.g., `@contextmanager` changing the return to a context manager) are handled via a configurable decorator registry in `.agon/config.toml`.

**Mutable default arguments.** `def f(x=[])` is stateful — the default list persists across calls. The purity detector must flag mutable defaults (`list`, `dict`, `set` in default values) as a purity violation, even when no other impurity signal is present.

---

## 5. Component: mutagen — Mutation Testing

### 5.1 Purpose

Measure whether a codebase's test suite would detect behavioral changes. Where eigentest defines what "correct" means, mutagen measures whether the tests actually enforce it. The mutation score — the proportion of generated behavioral changes that tests catch — is a more honest quality metric than line coverage.

### 5.2 Why Invariant-Guided Mutation Matters

Traditional mutation testing applies blind operators (negate boolean, swap `+` and `-`, delete statement). This generates a large number of equivalent mutants — mutations that don't change observable behavior — which waste compute and dilute the signal.

Invariant-guided mutation generates mutations that are *designed* to violate a specific invariant. If the invariant is "returns a value in {200, 400, 500}", mutagen generates a mutation that returns 503. This mutation is non-equivalent by construction: it violates a stated property. The mutation score then measures how well the test suite enforces that specific property.

This dramatically improves the signal-to-noise ratio. Instead of "73% of random mutations were killed," the report says "the invariant 'never returns True on expired tokens' has a mutation score of 40% — your tests would miss 3 out of 5 ways this could break."

### 5.3 Internal Architecture

```
            ┌────────────────────────────────┐
            │  Input: FunctionRef +           │
            │         Invariant[]             │
            └───────────────┬────────────────┘
                            │
            ┌───────────────▼────────────────┐
            │    AST Parsing                 │
            │    (tree-sitter → libCST)      │
            └───────────────┬────────────────┘
                            │
          ┌─────────────────┴──────────────────┐
          ▼                                    ▼
  ┌────────────────────┐           ┌───────────────────────┐
  │ Mechanical Operator│           │  LLM-Guided Operator  │
  │   Generator        │           │    Selector            │
  │                    │           │                        │
  │  Enumerate all     │           │  Given invariants +    │
  │  applicable AST    │           │  function context,     │
  │  mutations         │           │  rank and filter the   │
  │                    │           │  mechanical mutations  │
  │                    │           │  + propose semantic    │
  │                    │           │  mutations             │
  └────────┬───────────┘           └──────────┬────────────┘
           │                                  │
           └──────────────┬───────────────────┘
                          │ Mutation[] (candidate)
                          ▼
            ┌─────────────────────────────────┐
            │   Equivalent Mutant Filter      │
            │                                 │
            │  Stage 1: LLM classification    │
            │  Stage 2: empirical (run tests  │
            │    on uncertain cases)          │
            └───────────────┬─────────────────┘
                            │ Mutation[] (non-equivalent)
                            ▼
            ┌─────────────────────────────────┐
            │   Impact Analyzer               │
            │                                 │
            │  Map each mutation to the       │
            │  subset of tests that exercise  │
            │  the mutated code               │
            └───────────────┬─────────────────┘
                            │ (mutation, test_subset)[]
                            ▼
            ┌─────────────────────────────────┐
            │   Sandboxed Executor            │
            │                                 │
            │  Run test_subset against each   │
            │  mutation in isolated process   │
            │  with timeout and resource      │
            │  limits                         │
            └───────────────┬─────────────────┘
                            │
                            ▼
                      Mutation[]
              (status: killed | survived |
               equivalent | timeout)
```

### 5.4 AST Layer

**Parser: tree-sitter** — used for all initial parsing and mutation site identification. Tree-sitter is language-agnostic, fast, and has robust error recovery. It provides a uniform AST across languages, which is the foundation for multi-language support.

**Code transformer: libCST (Python v1)** — used for format-preserving code modifications. When mutagen changes `>=` to `>`, it must preserve whitespace, comments, and formatting so the diff is clean and reviewable.

The separation is deliberate: tree-sitter identifies *what* to mutate, libCST performs the mutation. This means the tree-sitter analysis can be reused across languages, while only the transformation backend changes.

**Language adapter interface — the shared abstraction layer:**

The `LanguageAdapter` protocol is the single point of language-specific logic in Agon. Both eigentest and mutagen consume a LanguageAdapter. Neither imports tree-sitter, libCST, or any language-specific package directly. **Nothing outside `src/agon/adapters/` should import language-specific packages.** This is the boundary that enables multi-language support without rewriting eigentest, mutagen, or spectre.

```python
class LanguageAdapter(Protocol):
    def parse(self, source: str) -> tree_sitter.Tree: ...
    def get_functions(self, tree: tree_sitter.Tree) -> list[FunctionNode]: ...
    def apply_mutation(self, source: str, mutation: Mutation) -> str: ...
    def get_type_info(self, func: FunctionNode) -> TypeInfo: ...
    def extract_test_assertions(self, test_source: str) -> list[TestAssertion]: ...
    def run_tests(self, project_root: Path, test_filter: list[str] | None) -> TestResult: ...
    def get_coverage(self, project_root: Path) -> CoverageMap | None: ...
```

**v1 implementation:** `PythonAdapter` using tree-sitter-python + libCST + pytest + coverage.py.

**vN:** `TypeScriptAdapter` (tree-sitter-typescript + ts-morph + Jest/Vitest), `GoAdapter` (tree-sitter-go + go/ast + `go test`), `RustAdapter` (tree-sitter-rust + syn + `cargo test`).

### 5.5 Mutation Operator Catalog

Operators are classified into two tiers:

**Tier 1 — Mechanical operators** (no LLM, exhaustively enumerable):

| Operator | Example | Target Invariant Category |
|----------|---------|--------------------------|
| `comparison_boundary` | `>=` → `>`, `<` → `<=` | value_domain, precondition |
| `boolean_negate` | `and` → `or`, `not x` → `x` | precondition, postcondition |
| `arithmetic_swap` | `+` → `-`, `*` → `/` | relational |
| `constant_replace` | `0` → `1`, `True` → `False` | value_domain |
| `return_value_replace` | `return x` → `return None` | postcondition, value_domain |
| `statement_delete` | Remove a line | postcondition, exception |
| `exception_swallow` | `raise X` → `pass` | exception |
| `condition_negate` | `if x:` → `if not x:` | precondition |

**Tier 2 — LLM-guided operators** (require semantic context):

| Operator | Description | When Used |
|----------|-------------|-----------|
| `boundary_shift` | Off-by-one in loop bounds or comparisons | When invariant specifies a boundary condition |
| `guard_removal` | Delete an edge case check | When invariant covers an edge case |
| `default_mutation` | Change a default parameter value | When invariant depends on defaults |
| `type_coercion` | Change type casting behavior | When invariant is a type constraint |
| `semantic_swap` | Replace a value with a semantically related but wrong value (e.g., 200 → 201) | When invariant specifies exact values |

**LLM site selection:** The local model receives the function AST and its invariants, and outputs a ranked list of (mutation_site, operator, target_invariant) triples. This focuses mutation effort on the sites most likely to expose test weaknesses.

### 5.6 Equivalent Mutant Handling

Equivalent mutants — mutations that don't change observable behavior — are the primary source of noise in mutation testing. Detecting them is undecidable in the general case.

**Agon's two-stage approach:**

1. **LLM pre-filter.** Before running any tests, the local model classifies each candidate mutation as "likely equivalent" or "likely non-equivalent." This is fast and cheap. Mutations classified as "likely equivalent" are set aside.

2. **Empirical fallback.** Mutations classified as uncertain (confidence < 0.7 in either direction) are run against the test suite anyway, with a tight timeout. If the test suite passes (the mutant survives), the mutation might be equivalent — or might be a genuine test gap. This ambiguity is surfaced in the report.

3. **Accuracy tracking.** Agon tracks the LLM's equivalent-mutant classification accuracy over time by comparing predictions against empirical results. If accuracy drops below a configurable threshold, the pre-filter is bypassed and all mutations are tested empirically.

### 5.7 Impact Analysis

Running the full test suite per mutation is O(N x M) and quickly becomes prohibitive. Impact analysis reduces this to O(n x M) where n << N.

**Implementation:**

1. **Static dependency graph.** Parse import statements and call sites to build a function → test mapping. If `test_auth.py` imports and calls `auth.validate()`, then mutations to `validate()` only need to run tests in `test_auth.py`.

2. **Coverage-based mapping (when available).** If coverage data exists from a prior test run, use it for precise test → line mapping. A mutation at line 42 only runs tests whose coverage data includes line 42.

3. **Fallback.** If no coverage data and static analysis is ambiguous, run the full suite but with early termination: stop as soon as any test fails (the mutant is killed; no need to run remaining tests).

**Fixture scope interaction:** When running a test subset, pytest fixtures with `scope="session"` or `scope="module"` may behave differently than in a full run. A session-scoped fixture initializing a database connection runs once per subset invocation, not once per full suite. To mitigate: the test subset is expanded to include all tests sharing a session/module-scoped fixture with any selected test. If subset expansion exceeds 50% of the full suite, fall back to full-suite execution (the impact analysis savings are negligible at that point).

### 5.8 Sandboxed Execution

Mutated code can be dangerous. A mutation that removes an `if` guard might cause infinite recursion. A mutation that changes a file path might write to the wrong location.

**Threat model:** Accidental damage (infinite loops, file writes, state corruption), not adversarial VM escape. This distinction matters — it rules out heavy solutions like Firecracker microVMs which are designed for multi-tenant adversarial isolation.

**Sandbox backend protocol:**

```python
class SandboxBackend(Protocol):
    def execute(self, mutated_source: str, target_file: str,
                test_command: str, timeout: float,
                memory_limit_mb: int) -> ExecutionResult: ...
```

**Backend comparison:**

| Backend | Isolation | Startup | Platform | When to Use |
|---------|-----------|---------|----------|-------------|
| ProcessSandbox | Weak (same user) | ~5ms | All | v1 default, local dev |
| ContainerSandbox (Docker/Podman) | Strong | ~200ms | Linux, macOS, Win | Production local, team use |
| NamespaceSandbox (nsjail/bubblewrap) | Strong | ~10ms | Linux only | CI, fastest strong isolation |
| CloudSandbox (e2b.dev) | Very strong (remote) | ~2-5s | Any (API) | CI without container runtime |

**v1 execution sandbox (ProcessSandbox):**

```
Per-mutation execution:
  1. Copy the target file to a temp directory (tmpfs if available)
  2. Apply the mutation to the copy
  3. Fork a subprocess with:
     - Timeout: 2x the normal test suite duration (configurable, default 30s)
     - Memory limit: 1 GB (configurable)
     - Working directory: temp directory
     - Environment: allowlisted variables only (PATH, HOME, PYTHONPATH,
       VIRTUAL_ENV, LANG, TERM, USER, and test-runner-specific vars
       declared in config). All other variables are blocked — an allowlist
       is safer than stripping known credential prefixes, since applications
       store secrets under unpredictable names.
     - Network: disabled via environment variables at minimum
  4. Run the affected test subset
  5. Collect exit code + stdout/stderr
  6. Kill the process if timeout exceeded
  7. Clean up temp directory
```

**Import-time side effects:** Python modules can execute arbitrary code at import time — registering signal handlers, opening connections, reading config files. When mutagen copies a file and imports it in a subprocess, these side effects fire. The allowlisted environment limits damage, and the sandbox's working directory (temp, not project root) prevents most path-relative reads. Modules with known import-time side effects should be added to `skip_patterns` in configuration.

**v2 sandbox options:**
- **ContainerSandbox:** OCI containers (Docker/Podman). Read-only bind mount of source tree, writable overlay for mutation. Network disabled. Cgroup resource limits. Cross-platform.
- **NamespaceSandbox:** nsjail or bubblewrap on Linux. Fastest strong isolation (~10ms startup). Syscall filtering via seccomp. Preferred for CI on Linux.
- **CloudSandbox:** e2b.dev or similar. Per-mutation latency (~2-5s) makes this impractical for high-volume local mutation testing. Better suited for batch mode: spin up a cloud instance, run all mutations there, tear down. Useful for CI environments without container runtimes.

The backend is configurable in `.agon/config.toml` under `[sandbox]`.

### 5.9 Mutation Score Reporting

Mutation scores are reported at two levels:

1. **Per-function score:** `killed / (total - equivalent)` for all mutations targeting the function.
2. **Per-invariant score:** `killed / (total - equivalent)` for all mutations targeting a specific invariant.

The per-invariant score is the key insight. It answers: "Is this specific behavioral property enforced by the test suite?" A function might have 90% overall mutation score but 0% score for a critical security invariant — that's an actionable finding.

### 5.10 Testing mutagen

**Unit tests:**
- Each mechanical operator applied to reference functions → verify valid syntax and expected behavior change.
- Known killed mutation → verify tests catch it.
- Known equivalent mutation → verify classified correctly (or at least not reported as survived).

**Integration tests:**
- Reference codebase with seeded bugs and known mutation scores → verify mutagen produces the expected scores within tolerance.
- Performance benchmark: mutations per second on reference codebase.

**Property tests:**
- For any function F and mutation M, `apply_mutation(F, M)` produces syntactically valid code.
- For any non-equivalent mutation M, there exists at least one input where `F(input) != M(input)`.

### 5.11 Handling Abstractions (ORMs, Generated Code)

Code that developers didn't write requires different treatment:

| Category | v1 Strategy | Rationale |
|----------|-------------|-----------|
| Generated code (ORM model definitions, protobuf stubs, auto-generated serializers) | **Skip** — use `skip_patterns` in config | Generator's own test suite is responsible |
| Developer-written ORM queries (`User.objects.filter(active=True)`) | **Mutate** — treat the ORM API as a trusted boundary | The query is developer logic; the ORM is infrastructure |
| Integration behavior (does the query hit the right table?) | **Out of scope for v1** | Requires execution harness with real or mock DB |

For vN: LLM-generated mock/stub fixtures for ORM layers, enabling mutation testing of code that interacts with databases without requiring a live DB connection.

### 5.12 Future Additions

1. **Higher-order mutation.** Combine multiple operators in a single mutant. Harder to kill, more realistic (real bugs rarely involve a single character change).
2. **Cross-function mutation.** Change a callee's behavior and check if the caller's tests catch it. Tests contract compliance across module boundaries.
3. **Git-aware mutation priority.** Weight recently modified code higher — newer code is statistically more likely to contain bugs.
4. **Mutation operator learning.** Track which operators produce the most surviving mutants per codebase. Prioritize those operators in future runs.
5. **Parallel worker pool.** Distribute mutation execution across multiple machines for large codebases.
6. **Cosmic-ray / mutmut integration.** Import their operator libraries as additional Tier 1 operators, expanding the mechanical catalog.

---

## 6. Component: spectre — Counterexample Generation

### 6.1 Purpose

For every surviving mutation (a behavioral change that the test suite fails to catch), spectre generates a concrete input that demonstrates the gap. The output is an executable test case that the developer can review, understand, and add to their test suite.

### 6.2 Operating Modes

Spectre supports three modes that correspond to increasingly strong verification:

**Mode 1 — Mutation-guided counterexample generation** (default):
Given a surviving mutant, find an input where the original and mutant produce different outputs, then validate whether the original's output satisfies the invariant. Requires one implementation, one mutant, and one invariant. Cheap and targeted — catches test gaps.

**Mode 2 — Reference implementation differential testing** (when spec is available):
LLM generates a reference implementation from the spec. Compare original vs. reference on generated inputs. This is classical differential testing. Catches spec-implementation drift directly — the original thesis. The reference implementation doesn't need to be production-quality; it just needs to be correct for the test inputs. More expensive (full code generation per function), higher value when specs exist.

**Mode 3 — Self-evaluation** (meta-level):
Use mutation analysis to evaluate spectre's own fuzzing corpus effectiveness. Generate mutations of the original, run spectre's input corpus against them, measure what % of mutations the corpus would catch. This is directly from the Gorz et al. framework ("Systematic Assessment of Fuzzers using Mutation Analysis"). Reports on how good spectre's inputs are, not just what bugs they find.

Mode 1 is always run. Mode 2 is run when a spec is available and `--differential` is passed. Mode 3 is run when `--self-eval` is passed (primarily for Agon's own development and tuning).

### 6.3 Internal Architecture

```
            ┌─────────────────────────────────┐
            │  Input: Mutation (survived) +    │
            │         Invariant (targeted)     │
            └───────────────┬─────────────────┘
                            │
            ┌───────────────▼─────────────────┐
            │   Input Strategy Selector       │
            │                                 │
            │  Based on function signature    │
            │  and invariant category,        │
            │  choose generation strategy     │
            └───────────────┬─────────────────┘
                            │
          ┌─────────────────┴──────────────────┐
          ▼                                    ▼
  ┌────────────────────┐           ┌───────────────────────┐
  │ Type-Driven        │           │  LLM-Augmented        │
  │  Generator         │           │   Generator            │
  │                    │           │                        │
  │  Hypothesis-style  │           │  LLM examines the     │
  │  strategies based  │           │  mutation + invariant  │
  │  on type annot.    │           │  and crafts targeted   │
  │                    │           │  inputs designed to    │
  │  Boundary values,  │           │  distinguish mutant    │
  │  edge cases,       │           │  from original         │
  │  random samples    │           │                        │
  └────────┬───────────┘           └──────────┬────────────┘
           │                                  │
           └──────────────┬───────────────────┘
                          │ candidate_inputs[]
                          ▼
            ┌─────────────────────────────────┐
            │   Executor                      │
            │                                 │
            │  For each input:                │
            │    run original(input) → O      │
            │    run mutant(input)   → M      │
            │    if O ≠ M → distinguishing    │
            │      input found                │
            └───────────────┬─────────────────┘
                            │
                            ▼
            ┌─────────────────────────────────┐
            │   Dual-Oracle Validation        │
            │                                 │
            │  Oracle 1: does O satisfy the   │
            │    invariant?                   │
            │  Oracle 2: does O satisfy the   │
            │    spec (if available)?         │
            │                                 │
            │  Agreement → high confidence    │
            │  Disagreement → flag for review │
            └───────────────┬─────────────────┘
                            │
                            ▼
                    Counterexample
```

### 6.4 Type-Driven Input Generation

For v1 (pure functions), spectre generates inputs based on type annotations using strategies similar to Hypothesis:

| Type | Strategy |
|------|----------|
| `int` | 0, 1, -1, MAX_INT, MIN_INT, boundary ± 1, random |
| `float` | 0.0, -0.0, 1.0, -1.0, inf, -inf, NaN, epsilon, random |
| `str` | `""`, `" "`, unicode, control chars, very long, `None`-like ("null", "None") |
| `bool` | `True`, `False` |
| `list[T]` | `[]`, `[single]`, sorted, reverse-sorted, duplicates, very long |
| `dict[K, V]` | `{}`, single entry, large, nested |
| `Optional[T]` | `None` + all strategies for `T` |
| `enum` | all members + (if possible) invalid values |
| Custom class | Recursive generation from `__init__` signature |

Boundary values are derived from the targeted invariant. If the invariant says `amount > 0`, spectre specifically generates `0`, `-1`, and `1` as inputs.

**Floating-point comparison.** For functions returning `float`, the executor uses epsilon-aware comparison (`abs(a - b) < epsilon`) rather than strict equality when checking `original(x) != mutant(x)`. NaN requires special handling: `NaN != NaN` in IEEE 754, so a naive distinguishing-input check always reports "different" when both return NaN. The executor detects NaN outputs and treats `(NaN, NaN)` as equivalent. The default epsilon is `1e-9`, configurable per-function via invariant metadata.

### 6.5 LLM-Augmented Input Generation

The type-driven generator covers structural edge cases but misses semantic ones. The LLM receives:

- The original function code
- The mutated function code (with the mutation highlighted)
- The targeted invariant
- The type-driven inputs already generated (to avoid duplicates)

Task: generate 3-5 additional inputs specifically designed to distinguish the mutant from the original. Focus on the semantic meaning of the mutation.

Example: if the mutation changes `token.expiry > now` to `token.expiry >= now`, the LLM should generate an input where `token.expiry == now` (the exact boundary the mutation shifts).

**Model diversity:** The model used for input generation should differ from the model used for invariant inference. This ensures that spectre's inputs can catch blind spots in eigentest's invariants.

### 6.6 Dual-Oracle Validation

A distinguishing input proves the mutant differs from the original. But does the original's behavior satisfy the invariant?

**Oracle 1: Invariant check.** Run the invariant's `property_code` against the original's output. If it fails, the original code may have a bug — not just a test gap.

**Oracle 2: Spec check (when available).** If eigentest produced spec-derived invariants, check the original's output against the spec-derived expectation. This is the independent oracle that breaks circularity.

**Numeric tolerance:** When oracle checks involve numeric comparison, the same epsilon-aware and NaN-aware semantics from Section 6.4 apply. Invariant `property_code` that asserts equality on float values is automatically wrapped in approximate comparison.

**Outcome matrix:**

| Original satisfies invariant? | Original satisfies spec? | Interpretation |
|-------------------------------|--------------------------|----------------|
| Yes | Yes | Test gap — the mutation should have been caught |
| Yes | No | Spec drift — code works but diverges from spec |
| No | Yes | Bug — code violates spec and invariant missed it |
| No | No | Bug — code is wrong and tests don't catch it |
| Yes | N/A (no spec) | Test gap (lower confidence) |
| No | N/A (no spec) | Possible bug (flag for human review) |

### 6.7 Output: Reproducer Generation

Each counterexample includes executable test code:

```python
def test_counterexample_a1b2c3():
    """
    Generated by Agon.
    Invariant: process_payment returns status code in {200, 400, 500}
    Mutation: return 503 (boundary addition to status set)
    This mutation survived — no existing test catches this behavioral change.
    """
    result = process_payment(amount=-1)
    assert result in (200, 400, 500), f"Expected status in {{200, 400, 500}}, got {result}"
```

The developer can copy this test directly into their test suite. The docstring explains *why* this test exists, not just what it checks.

### 6.8 Testing spectre

**Unit tests:**
- Type-driven generator produces valid inputs for each supported type.
- Distinguishing input finder correctly identifies inputs where `f(x) != mutant(x)` on seeded mutations.

**Integration tests:**
- Reference codebase with known surviving mutations and known counterexamples → verify spectre finds them.
- Reproducer code executes without errors and correctly asserts the property.

**Property tests:**
- For any generated input and function, the input is a valid instance of the function's parameter types.
- Generated reproducer code is syntactically valid Python.

### 6.9 Future Additions

1. **Stateful API testing.** Extend beyond pure functions. Generate sequences of API calls with state assertions between them. Requires an execution harness that manages setup/teardown.
2. **Concolic execution integration.** Use constraint solvers (Z3, CVC5) to systematically find inputs that reach specific code paths, rather than relying on type-driven heuristics or LLM guessing.
3. **Metamorphic relation testing.** Instead of checking `f(x) == expected`, check `f(x) relates_to f(transform(x))`. Example: `sort([1,2,3]) == sort([3,2,1])`. This catches bugs where the absolute oracle is unknown but the relational property is certain.
4. **Coverage-guided feedback.** Track which code paths generated inputs exercise. Steer generation toward uncovered paths (AFL/libFuzzer-style feedback loop).
5. **Multi-implementation differential testing.** When multiple implementations exist (e.g., a Python prototype and a Go rewrite), compare their outputs on the same inputs.
6. **Test suite mutation analysis.** Mutate test files themselves to detect weak assertions. A test with `assert f(x) == wrong_value` passes against buggy code — mutating the test (e.g., changing the expected value) and checking whether the test suite still passes exposes assertions that are not actually verifying behavior.

---

## 7. Pipeline Orchestration

### 7.1 Execution Modes

**Single-pass mode** (default):
```
agon analyze <path>
```
Runs eigentest → mutagen → spectre once and produces a report.

**Iterative mode** (closed feedback loop):
```
agon analyze <path> --iterate
```
Runs the pipeline repeatedly, refining results:

```
Iteration 1:
  eigentest: infer invariants → I₁
  mutagen: generate mutations for I₁, run tests → survived₁
  spectre: generate counterexamples for survived₁ → C₁
  
  Update confidence scores:
    invariants whose mutations were killed → confidence++
    invariants with counterexamples showing original is wrong → confidence-- or remove
    
Iteration 2:
  eigentest: re-evaluate low-confidence invariants, add invariants
             suggested by counterexample patterns
  mutagen: generate new mutations targeting refined invariants
  spectre: generate counterexamples for new survivors
  
  ...continue until termination
```

**Component-only mode:**
```
agon eigentest <path>     # invariant inference only
agon mutagen <path>       # mutation testing only (accepts invariant file or uses built-in operators)
agon spectre <path>       # counterexample generation only (accepts mutation report)
```

### 7.2 Termination Conditions

Iterative mode terminates when any of these conditions is met (configurable):

| Condition | Default | Rationale |
|-----------|---------|-----------|
| Max iterations | 3 | Diminishing returns are steep after 2-3 passes |
| Mutation score threshold | 0.90 | 90% of targeted mutations killed = strong suite |
| Diminishing returns | < 5% improvement | Less than 5% score improvement vs. previous iteration |
| Budget limit (API cost) | $5.00 | Prevents runaway LLM spending |
| Time limit | 30 min | Wall-clock limit for CI contexts |

### 7.3 Incremental / Diff-Aware Mode

```
agon diff              # analyze only functions changed since last commit
agon diff --base main  # analyze only functions changed vs. main branch
```

**Implementation:**
1. Compute `git diff` to identify changed files and line ranges.
2. Map changed lines to affected functions (tree-sitter parsing).
3. Expand scope to include direct callers of changed functions (1-hop in the call graph — a change to a callee may break a caller's invariant).
4. Run the pipeline on the expanded scope only.
5. Compare mutation scores against stored baseline to detect regressions.

**Baseline storage:** After each full run, agon stores the mutation scores per function and per invariant in `.agon/baseline-<branch>.json` (branch-scoped). Incremental runs compare against this baseline and report deltas. Branch scoping prevents concurrent runs on different branches from overwriting each other's baselines. On the main branch, baseline writes use atomic file replacement (write to temp, rename) to prevent partial reads from concurrent CI jobs.

### 7.4 Prioritization and Triage

When the pipeline produces many findings, prioritization determines what the developer sees first.

**Severity assignment:**

| Condition | Severity |
|-----------|----------|
| Surviving mutation in security-sensitive function (auth, crypto, validation) | Critical |
| Surviving mutation violating a spec-derived invariant | Critical |
| Both oracles agree the original code is wrong | Critical |
| Surviving mutation in public API surface | High |
| Surviving mutation in high fan-in function | High |
| Surviving mutation in complex function (cyclomatic complexity > 10) | Medium |
| Surviving mutation in internal utility | Low |
| Surviving mutation where equivalent-mutant confidence > 0.5 | Low |

The report is sorted by severity. In CI mode, only Critical and High findings fail the check.

### 7.5 CI Integration

**GitHub Actions (primary target):**

```yaml
# .github/workflows/agon.yml
name: Agon Analysis
on: [pull_request]
jobs:
  agon:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: agon-tool/action@v1
        with:
          mode: diff
          base: ${{ github.event.pull_request.base.sha }}
          fail-on: critical,high
```

**Output formats:**
- **Terminal** — colored summary with inline code snippets and actionable suggestions. Default for CLI.
- **JSON** — machine-readable `AgonReport` for programmatic consumption.
- **SARIF** — for GitHub Code Scanning integration. Findings appear as annotations on the PR diff.
- **Markdown** — for PR comment integration. Summary table + top findings.

**vN:** GitLab CI, Jenkins, Buildkite support. IDE plugins (VS Code, JetBrains) that show invariants and mutation scores inline.

---

## 8. ML/AI Infrastructure

### 8.1 Model Diversity Principle

The central thesis of Agon is that correlated blind spots between code generation and code testing create a false sense of correctness. Agon must not recreate this problem internally.

**Rule:** No two adjacent pipeline stages should use the same model family for their LLM-dependent operations. The model that infers invariants (eigentest Stage 2) must differ from the model that generates counterexample inputs (spectre LLM generator).

**Default model assignment:**

| Role | Default Model | Category | Justification |
|------|---------------|----------|---------------|
| eigentest: mechanical extraction | N/A (no model) | Deterministic | Pure AST analysis |
| eigentest: pattern extraction (Stage 1) | Qwen2.5-Coder 7B (local) | SLM | High volume, low latency, zero API cost |
| eigentest: invariant validation (Stage 2) | Claude Sonnet | Frontier | Strong reasoning, different family from common code generators |
| eigentest: spec extraction | Claude Sonnet | Frontier | NLU quality for spec documents |
| mutagen: site selection | Qwen2.5-Coder 7B (local) | SLM | Cost-sensitive, high volume |
| mutagen: equivalent mutant filter | Claude Haiku | Frontier (fast) | Speed-sensitive, moderate accuracy sufficient |
| spectre: input generation | GPT-4o or Gemini | Frontier | Model diversity vs. eigentest's Claude |
| Embedding (semantic cache) | Voyage Code 3 | Embedding | Code-specialized, good structural similarity |

All model assignments are user-configurable. The system logs which model produced each artifact for traceability.

### 8.2 Local Model Infrastructure

Most pipeline operations use local models to control cost and latency.

**Deployment modes (all supported via unified OpenAI-compatible client):**

| Mode | Setup | Use Case | Cost |
|------|-------|----------|------|
| **Auto-detected local** | Agon checks for running Ollama, auto-pulls model if missing | Solo developer, zero-config | $0 (compute only) |
| **Configured remote endpoint** | User sets URL in config pointing to team vLLM server or any OpenAI-compatible API | Team environments, shared GPU | Shared infra cost |
| **API-only fallback** | No local model; all calls routed to frontier APIs with batching and caching | CI environments, no-GPU machines | Per-token API cost |

No custom protocol is needed. Ollama, vLLM, LiteLLM, and most inference servers expose the OpenAI chat completions format. The LLM client (`src/agon/llm/client.py`) accepts any compatible endpoint URL + optional API key.

**Quantization:** 4-bit GGUF for developer machines (fits in 8GB RAM). FP16 for server deployments with GPU.

**Model lifecycle:** Agon ships with a model manifest listing required models and their minimum quantization levels. On first run, `agon setup` offers to pull models via Ollama. This is optional — all model configuration is overridable in `.agon/config.toml`.

### 8.3 Semantic Cache Infrastructure

**v1: LanceDB (embedded)**
- Zero infrastructure. Single file on disk.
- Python-native. No server process.
- Supports cosine similarity search.
- Good enough for single-developer, single-project usage.

**vN: Qdrant (server)**
- Multi-user, multi-project.
- Persistent storage with HNSW indexing.
- Supports filtering (e.g., "similar functions in Python only").
- Self-hosted or cloud.

**Embedding pipeline:**

```
Function → tree-sitter parse
         → normalize AST (replace identifiers with positional tokens,
           replace literals with type tokens)
         → serialize normalized AST to string
         → concatenate: normalized_ast + "|" + type_signature + "|" + docstring_or_empty
         → embed with code embedding model
         → store: (embedding, invariant_set, content_hash, timestamp)
```

The normalization step is critical. Two functions that differ only in variable names should have identical AST skeletons and therefore identical cache keys. This is what makes the cache useful across codebases.

### 8.4 Observability and Token Tracking

The cost cap (Section 9.3) requires a mechanism to measure actual spend. The LLM client (`src/agon/llm/client.py`) wraps every call with usage tracking as a cross-cutting concern.

**Per-call tracking record:**

```
LLMUsage {
  timestamp:       datetime
  model:           string       # actual model used
  pipeline_stage:  string       # "eigentest_stage1", "mutagen_filter", etc.
  function_ref:    string?      # which function this call was about
  input_tokens:    int
  output_tokens:   int
  cost_usd:        float        # computed from model pricing table
  latency_ms:      int
  cache_hit:       bool         # was this served from semantic cache?
}
```

**v1: SQLite storage** — `.agon/usage.db`, single table, zero external dependencies. Enables:
- `agon stats` — cumulative usage by stage, model, and time period
- Budget enforcement across incremental runs (sum cost since last reset)
- Cost-per-function reporting in AgonReport

**vN: OpenTelemetry exporter** — emit spans and metrics in OTLP format, consumable by LangFuse, Datadog, Prometheus, Grafana, or any OTLP-compatible backend. This makes Agon observable within whatever monitoring stack the team already runs. No lock-in to a specific observability vendor.

### 8.5 Cost Management

| Mechanism | Effect |
|-----------|--------|
| Local models for 80%+ of operations | Eliminates API cost for routine work |
| Semantic cache | Avoids re-inferring invariants for similar functions |
| Impact analysis | Reduces test executions per mutation from O(N) to O(n) |
| Budget limits (configurable) | Hard cap on API spend per run |
| Batching | Group multiple LLM queries into single API calls where possible |
| Incremental mode | Only analyze changed code |

**Cost estimation (rough, for a 500-function Python project, full run):**

| Component | API Calls | Estimated Cost |
|-----------|-----------|---------------|
| eigentest Stage 1 (local) | 0 | $0.00 |
| eigentest Stage 2 (frontier, ~100 functions after cache/priority filtering) | ~100 | $0.50 - $2.00 |
| mutagen site selection (local) | 0 | $0.00 |
| mutagen equivalent filter (Haiku) | ~200 | $0.10 - $0.30 |
| spectre input generation (frontier) | ~50 | $0.25 - $1.00 |
| Embeddings | ~500 | $0.02 |
| **Total** | | **$0.87 - $3.32** |

Incremental runs (diff-aware) cost ~10-20% of a full run.

Cost ranges reflect variance in function complexity and prompt length. Simple functions (2-3 parameters, linear control flow) use ~500 tokens per LLM call; complex functions (many branches, nested logic, long docstrings) use ~3000 tokens. The 4x range in eigentest Stage 2 is driven by this per-function variance, not model pricing uncertainty.

---

## 9. Implementation Language and Project Structure

### 9.1 Language: Python

Rationale:
- The v1 target ecosystem is Python. Using Python for the tool itself means direct access to `libCST`, `coverage.py`, `pytest`, `hypothesis`, and the entire Python AST ecosystem without FFI overhead.
- LLM API clients (Anthropic, OpenAI, Ollama) are Python-first.
- Rapid iteration matters for an experimental tool. Python's development speed outweighs runtime performance concerns — the performance-critical path is test execution (subprocess calls), not Agon's own code.
- tree-sitter has excellent Python bindings (`tree-sitter` package).

**vN:** Rust extensions (via PyO3) for AST manipulation hot paths if profiling shows they're bottlenecks. The `LanguageAdapter` interface is designed so that a Rust-backed adapter can be a drop-in replacement.

### 9.2 Project Structure

```
agon/
├── pyproject.toml
├── src/
│   └── agon/
│       ├── __init__.py
│       ├── cli.py                    # CLI entry point (click or typer)
│       ├── config.py                 # Configuration loading and validation
│       ├── models/
│       │   ├── __init__.py
│       │   ├── schema.py             # Core data model (Invariant, Mutation, etc.)
│       │   └── report.py             # AgonReport generation and serialization
│       ├── eigentest/
│       │   ├── __init__.py
│       │   ├── engine.py             # Orchestrates the eigentest pipeline
│       │   ├── mechanical.py         # Deterministic invariant extraction
│       │   ├── llm_chain.py          # Two-stage LLM inference chain
│       │   ├── spec_extractor.py     # Spec document parsing
│       │   └── cache.py              # Semantic cache operations
│       ├── mutagen/
│       │   ├── __init__.py
│       │   ├── engine.py             # Orchestrates mutation testing
│       │   ├── operators/
│       │   │   ├── __init__.py
│       │   │   ├── mechanical.py     # Tier 1 operators
│       │   │   └── llm_guided.py     # Tier 2 operators
│       │   ├── filter.py             # Equivalent mutant filtering
│       │   ├── impact.py             # Impact analysis / test selection
│       │   └── executor.py           # Sandboxed mutation execution
│       ├── spectre/
│       │   ├── __init__.py
│       │   ├── engine.py             # Orchestrates counterexample generation
│       │   ├── generators/
│       │   │   ├── __init__.py
│       │   │   ├── type_driven.py    # Hypothesis-style input generation
│       │   │   └── llm_augmented.py  # LLM-guided input generation
│       │   ├── oracle.py             # Dual-oracle validation
│       │   └── reproducer.py         # Executable test code generation
│       ├── adapters/
│       │   ├── __init__.py
│       │   ├── base.py               # LanguageAdapter protocol
│       │   └── python.py             # Python adapter (tree-sitter + libCST + pytest)
│       ├── llm/
│       │   ├── __init__.py
│       │   ├── client.py             # Unified LLM client (local + API, OpenAI-compat)
│       │   ├── prompts.py            # Prompt templates per task
│       │   ├── diversity.py          # Model assignment and diversity enforcement
│       │   └── usage.py              # Token tracking, cost computation, SQLite persistence
│       ├── sandbox/
│       │   ├── __init__.py
│       │   ├── base.py               # SandboxBackend protocol
│       │   ├── process.py            # ProcessSandbox (v1 default)
│       │   ├── container.py          # ContainerSandbox (Docker/Podman, vN)
│       │   └── cloud.py              # CloudSandbox (e2b.dev, vN)
│       ├── pipeline.py               # Pipeline orchestration and feedback loop
│       ├── incremental.py            # Git-diff-aware scoping
│       └── priority.py               # Function prioritization scoring
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── fixtures/                     # Reference codebases for testing
│   │   ├── clean_functions/          # Functions with known-correct invariants
│   │   ├── buggy_functions/          # Functions with known bugs
│   │   └── edge_cases/              # Concurrency, floats, etc.
│   └── conftest.py
└── .agon/                            # Runtime state (gitignored)
    ├── cache/                        # Semantic cache (LanceDB)
    ├── baseline.json                 # Stored mutation scores
    ├── usage.db                      # Token/cost tracking (SQLite)
    └── config.toml                   # User configuration
```

### 9.3 Configuration

```toml
# .agon/config.toml

[general]
language = "python"                    # Target language
test_command = "pytest"                # How to run tests
timeout_seconds = 30                   # Per-mutation timeout
max_iterations = 3                     # Feedback loop iterations
budget_limit_usd = 5.00               # API cost cap

[models]
eigentest_local = "qwen2.5-coder:7b"  # Ollama model name
eigentest_frontier = "claude-sonnet-4-6"
mutagen_local = "qwen2.5-coder:7b"
mutagen_filter = "claude-haiku-4-5-20251001"
spectre_generator = "gpt-4o"
embedding = "voyage-code-3"

[cache]
backend = "lancedb"                    # "lancedb" or "qdrant"
similarity_threshold = 0.88
path = ".agon/cache"

[priority]
skip_patterns = ["**/migrations/**", "**/generated/**", "**/vendor/**"]
critical_patterns = ["**/auth/**", "**/crypto/**", "**/security/**"]

[sandbox]
backend = "process"                    # "process" | "container" | "cloud"
timeout_multiplier = 2.0               # mutation timeout = normal suite time * this
memory_limit_mb = 1024

[observability]
backend = "sqlite"                     # "sqlite" | "otlp"
path = ".agon/usage.db"
# otlp_endpoint = "http://localhost:4317"  # for vN OpenTelemetry export

[ci]
fail_on = ["critical", "high"]
output_format = "sarif"
```

**Monorepo support (vN):** For monorepos with multiple packages, each package can have its own `.agon/config.toml` with independent settings, baselines, and caches. The CLI accepts `--config <path>` to specify which config to use. A root-level `.agon/config.toml` can define shared settings (model assignments, budget limits) that per-package configs inherit and override.

---

## 10. Build Phases

### Phase 0 — Foundation (weeks 1-3)

**Goal:** Core data model, tree-sitter integration, and project skeleton.

- Implement `schema.py` with all data model types and JSON serialization.
- Implement `PythonAdapter` with tree-sitter-python parsing, function extraction, and pytest test runner integration.
- Implement `cli.py` with the command structure (subcommands stubbed).
- Implement `config.py` with TOML loading and validation.
- Set up test infrastructure with reference fixture codebases.

**Exit criteria:** `agon eigentest <path>` parses a Python file and outputs function signatures. `agon mutagen <path>` applies a single hardcoded mutation and runs pytest.

### Phase 1 — mutagen MVP (weeks 4-8)

**Goal:** A working mutation testing tool with mechanical operators.

- Implement all Tier 1 mechanical operators.
- Implement libCST-based mutation application.
- Implement sandboxed executor with timeout and process isolation.
- Implement basic impact analysis (static import tracing).
- Implement mutation score reporting (per-function).
- Wire up CLI: `agon mutagen <path>` produces a mutation report.

**Exit criteria:** Run mutagen on a 50-function Python project. Mutation score is computed. All mutations produce valid Python. Execution completes in < 5 minutes.

### Phase 2 — eigentest v1 (weeks 9-14)

**Goal:** Invariant inference from code, feeding into mutagen.

- Implement mechanical extractor (types, assertions, return values, test assertions, purity detection).
- Implement LLM inference chain (local model + frontier model).
- Implement semantic cache with LanceDB + embedding pipeline.
- Implement invariant → mutation mapping (per-invariant mutation score).
- Implement LLM-guided mutation site selection (Tier 2 operators).
- Implement equivalent mutant LLM pre-filter.
- Wire up CLI: `agon eigentest <path>` produces invariants. `agon analyze <path>` runs eigentest → mutagen.

**Exit criteria:** eigentest infers correct invariants for 70%+ of functions in the reference corpus. Per-invariant mutation scores are reported. Cache hit rate > 30% on second run of same codebase.

### Phase 3 — spectre v1 + Daikon (weeks 15-20)

**Goal:** Counterexample generation for surviving mutations on pure functions. Runtime invariant grounding via Daikon.

- Implement type-driven input generator for Python built-in types.
- Implement LLM-augmented input generator.
- Implement dual-oracle validation (invariant check + spec check when available).
- Implement reproducer code generation.
- Implement Daikon/DynaPyt integration: test instrumentation, trace collection, runtime invariant extraction with `source: "runtime_observed"`.
- Wire up CLI: `agon spectre <path>` generates counterexamples. `agon analyze <path>` runs the full pipeline.

**Exit criteria:** For surviving mutations on pure functions, spectre generates distinguishing inputs for 80%+ of cases. Reproducer code is syntactically valid and executable. When a test suite exists, Daikon produces runtime invariants that are merged into eigentest output.

### Phase 4 — Integration and Polish (weeks 21-26)

**Goal:** Closed feedback loop, incremental mode, CI integration, spec input.

- Implement iterative mode with termination conditions.
- Implement diff-aware incremental mode with baseline storage.
- Implement spec input channel (markdown and OpenAPI).
- Implement SARIF output.
- Implement GitHub Actions integration.
- Implement prioritization and severity assignment.
- Implement confidence score evolution across iterations.

**Exit criteria:** Full pipeline runs end-to-end in iterative mode. Incremental mode processes a 10-file diff in < 2 minutes. SARIF output renders correctly in GitHub Code Scanning.

### Phase 5 — Beyond v1 (ongoing)

- Human-in-the-loop web UI for invariant review.
- TypeScript language adapter.
- Metamorphic testing stage.
- Concolic execution integration (Z3/CVC5 constraint solving for spectre input generation).
- Cross-project semantic cache.
- Higher-order and cross-function mutation.
- Spectre reference implementation mode (Mode 2 — differential testing against LLM-generated reference).
- OpenTelemetry observability exporter.

---

## 11. Risk Register

| Risk | Likelihood | Impact | Mitigation | Owner |
|------|-----------|--------|------------|-------|
| False invariants codify bugs | High | High | Multi-source inference, spec grounding, confidence scoring, dual oracle | eigentest |
| Computational cost kills adoption | High | High | Impact analysis, selective mutation, incremental mode, local models | mutagen |
| Equivalent mutants waste compute | Medium | Medium | LLM pre-filter + empirical fallback + accuracy tracking | mutagen |
| LLM costs exceed value | Medium | Medium | Local models default, aggressive caching, budget limits | infra |
| Correlated blind spots across pipeline | Medium | High | Model diversity principle, mechanical baselines | infra |
| Sandbox escape by mutated code | Low | Critical | Process isolation, no network, no credentials, filesystem isolation | mutagen |
| Cold start produces useless results | Medium | Medium | Mechanical extraction baseline, bootstrap mode | eigentest |
| Alert fatigue from too many findings | Medium | High | Severity scoring, prioritization, incremental rollout | pipeline |
| Model quality regression on update | Medium | Medium | Snapshot regression tests, pinned model versions | infra |
| tree-sitter grammar bugs produce wrong AST | Low | Medium | Pin grammar versions, test against reference corpus | adapters |

---

## 12. Competitive Differentiation

| Existing Tool | What It Does | What Agon Adds |
|---------------|-------------|----------------|
| mutmut, cosmic-ray | Blind mutation testing | Invariant-guided mutations with per-invariant scoring |
| Hypothesis | Property-based testing (manual properties) | Automatic property inference |
| Daikon | Dynamic invariant detection from traces | Static + LLM inference, no instrumented runs needed |
| Meta's LLM mutation testing | LLM-guided mutation operators | Closed-loop pipeline connecting inference, mutation, and counterexample generation |
| CodiumAI / Qodo | AI test generation | Evaluates test *quality*, not just test quantity |
| Diffblue Cover | AI test generation (Java) | Language-agnostic design, mutation-based quality measurement |

Agon's differentiator is the closed loop. No existing tool connects invariant inference → mutation testing → counterexample generation into a single pipeline where each stage refines the signal from the previous one. Individual stages compete with specialized tools; the pipeline as a whole does not have a direct competitor.

---

## 13. Open Questions

These are decisions that require empirical data from v1 usage before they can be resolved:

1. **Optimal similarity threshold for semantic cache.** 0.88 is a starting default. Too high = cache misses, too low = wrong invariants applied. Needs tuning per codebase.

2. **Confidence floor for invariant inclusion in mutagen.** Should mutagen receive all invariants or only those above a confidence threshold? Lower threshold = more coverage but more noise. Starting default: 0.40.

3. **Equivalent mutant LLM accuracy threshold for bypassing pre-filter.** If the LLM's equivalent-mutant classification drops below X% accuracy, switch to empirical-only. Starting default: 70%.

4. **Ideal mutation count per function.** Too few = weak signal. Too many = high cost. The LLM site selector should produce a ranked list; the top-K are tested. Starting default: K=10.

5. **How to handle decorator-heavy code.** Python codebases using heavy decoration (Flask routes, FastAPI endpoints, pytest fixtures) may confuse invariant inference. Needs investigation.

6. **Value of the feedback loop.** Does iteration 2 actually improve results enough to justify the cost? If most value is captured in a single pass, the iterative mode may not be worth the complexity.

7. **Monorepo and multi-package support.** Should `.agon/config.toml` support per-package overrides with independent test suites, baselines, and invariant caches? This determines whether Agon works in monorepo-heavy organizations.

8. **Non-pure function strategy.** When eigentest encounters impure functions (I/O, state mutation, network), should it skip them, analyze with reduced confidence, or attempt limited invariant inference? The answer determines what fraction of a real codebase Agon covers on day one. (Section 4.10 documents the current strategy; empirical data will refine it.)

9. **Async function depth.** Async functions require special handling at every pipeline stage (Section 4.14). Should v1 support full async analysis or apply a limited extraction-only pass?

10. **Test file analysis.** Should Agon analyze test files to detect incorrect assertions (e.g., `assert f(x) == wrong_value` that passes against buggy code)? This is adjacent to the core mission but would significantly extend scope.

---

## References

- Gorz et al. "Systematic Assessment of Fuzzers using Mutation Analysis." USENIX Security 2023. https://www.usenix.org/system/files/usenixsecurity23-gorz.pdf
- mutants.rs — Mutation Testing vs Fuzzing comparison. https://mutants.rs/vs-fuzzing.html
- Meta — "Revolutionizing Software Testing: LLM-Powered Bug Catchers." https://engineering.fb.com/2025/02/05/security/revolutionizing-software-testing-llm-powered-bug-catchers-meta-ach/
- Ernst et al. "The Daikon system for dynamic detection of likely invariants." Science of Computer Programming, 2007. https://web.eecs.umich.edu/~weimerw/2024-481F/readings/daikon-tool-scp2007.pdf
- Fowler, Martin. "Cognitive Surrender." 2026. https://martinfowler.com/fragments/2026-04-02.html
- GitHub Spec Kit — Spec-Driven Development. https://github.com/github/spec-kit/blob/main/spec-driven.md
