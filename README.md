# Agon

Invariant-guided mutation testing and automated counterexample generation for Python codebases.

## What it does

Agon runs a three-phase pipeline against a codebase:

1. **Eigentest** — static analysis of function signatures, assertions, return values, raise patterns, and purity to infer behavioral invariants without executing any code.
2. **Mutagen** — systematic application of source mutations (operator swaps, condition negations, constant replacements, exception swallows, statement deletions) to each function, followed by test suite execution in isolated sandboxes to classify each mutant as killed or survived.
3. **Spectre** — for each survived mutation, generation of a structured counterexample: a severity-tagged reproducer stub that identifies the behavioral gap the test suite failed to catch.

The output is an `AgonReport` — a JSON artifact containing every inferred invariant, every executed mutation with its status, and every counterexample with a pytest-style reproducer. This report is suitable for CI gating, developer review, and incremental diffing across branches.

## Why mutation testing through invariants

Standard mutation testing asks: "do the tests kill this mutant?" Agon asks a narrower question first: "what are the behavioral contracts of this function?" — then generates mutations specifically designed to violate those contracts. This produces a smaller, higher-signal mutation set than exhaustive operator application and provides a direct link between a survived mutant and the invariant that it exposes as unverified.

Invariants are currently inferred mechanically (Phase 0/1). A planned LLM phase (Phase 2) will augment mechanical inference with semantic reasoning, and a dynamic phase (Phase 3) will synthesize concrete input/output pairs to make counterexamples executable rather than template-based.

## Architecture

```
CLI / GitHub Action
         |
    pipeline.run()
         |
   +-----+------+
   |            |
eigentest    (optional incremental filter via AgonReport cache)
   |
mutagen
   |
sandbox (ThreadPoolExecutor, isolated project copies)
   |
spectre
   |
AgonReport (JSON)
```

Each stage communicates through typed Pydantic models defined in `models/schema.py`. No stage has a direct dependency on any other stage's internal implementation — all coupling is through the shared schema and the `LanguageAdapter` protocol.

## Language adapter

All language-specific operations are behind a `LanguageAdapter` protocol:
- **Parsing:** tree-sitter for AST construction and function extraction
- **Mutation application:** libCST for syntactically-safe source transformation
- **Test execution:** subprocess invocation of the configured test runner (default: pytest)

The Python adapter is the reference implementation. New language adapters are added by implementing the protocol in `adapters/` and registering them in `adapters/factory.py`.

## Incremental mode

Agon tracks function identity by `(file, qualified_name, sha256(body))`. When a prior `AgonReport` is provided via `--cache`, functions whose body is unchanged carry forward their classified mutations without re-running the sandbox. This reduces CI wall time on large codebases where most functions are unchanged between commits.

## CI integration

`--fail-under <float>` exits 1 if the mutation score falls below the threshold. `ci.fail_on` in `config.toml` exits 1 if any counterexample reaches the specified severity levels (default: `critical`, `high`). The SARIF output format is compatible with GitHub Code Scanning.

## Configuration

Zero configuration required. Agon discovers project root via marker files (`.git`, `pyproject.toml`, etc.) and applies built-in defaults. Optional configuration lives in `.agon/config.toml`:

```toml
[general]
timeout_seconds = 30

[mutagen]
max_mutants_per_function = 20
parallel_workers = 4

[priority]
critical_patterns = ["**/auth/**", "**/payments/**"]
skip_patterns = ["**/migrations/**", "**/generated/**"]

[ci]
fail_on = ["critical", "high"]
```
