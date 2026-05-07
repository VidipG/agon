"""
Report persistence and incremental-run support.

Two responsibilities
--------------------
1. **Persistence**: save/load ``AgonReport`` as newline-terminated JSON so
   reports can be stored, diffed, and shipped to CI dashboards.

2. **Incremental filter**: given a current list of ``FunctionNode`` objects and
   a prior ``AgonReport``, skip re-running the sandbox for functions whose
   *content_hash* is unchanged.  Their prior ``Mutation`` results are carried
   forward; only genuinely changed (or new) functions are passed to the
   ``SandboxRunner``.

Incremental correctness guarantee
----------------------------------
A function is considered unchanged only when **all** of the following match
the prior report:
  - ``FunctionRef.file``         (relative path)
  - ``FunctionRef.name``         (qualified name)
  - ``FunctionRef.content_hash`` (sha256 of the function body)

If any field differs — the function was moved, renamed, or its body was
edited — it is treated as new and goes through a full sandbox run.

This is conservative: if tests change but function bodies do not, prior
mutation results are reused.  Add ``--no-cache`` (or delete the report file)
to force a clean run when you want to reflect new tests.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .adapters.base import FunctionNode
from .models.schema import AgonReport, Mutation, MutationStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def save_report(report: AgonReport, path: Path) -> None:
    """Write *report* to *path* as formatted JSON.

    Creates parent directories as needed.  Overwrites any existing file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    logger.debug("store: report saved to %s", path)


def load_report(path: Path) -> AgonReport:
    """Load and validate an ``AgonReport`` from *path*.

    Raises:
        FileNotFoundError: if *path* does not exist.
        ValidationError:   if the file is not a valid AgonReport.
    """
    return AgonReport.model_validate_json(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Incremental filter
# ---------------------------------------------------------------------------


def incremental_filter(
    current_functions: list[FunctionNode],
    current_mutations: list[Mutation],
    prior: AgonReport,
) -> tuple[list[FunctionNode], list[Mutation], list[Mutation]]:
    """Split work into (functions_to_run, mutations_to_run, carried_over_mutations).

    Functions whose ``(file, name, content_hash)`` matches a function recorded
    in *prior* are considered unchanged.  Their prior mutations — already
    classified as killed/survived/equivalent/etc. — are returned as
    ``carried_over_mutations`` and excluded from the sandbox run.

    Functions that are new or changed are included in ``functions_to_run``
    together with their pending ``mutations_to_run``.

    Args:
        current_functions: Functions discovered by EigentestEngine this run.
        current_mutations: Pending Mutation objects from MutagenEngine this run.
        prior: A previously saved AgonReport to compare against.

    Returns:
        (functions_to_run, mutations_to_run, carried_over_mutations)
    """
    # Use prior mutations to derive which (file, name) combos had stable results
    # Group prior mutations by function
    prior_by_func: dict[tuple[str, str], list[Mutation]] = {}
    for m in prior.mutations:
        if not m.function_refs:
            continue
        ref = m.function_refs[0]
        key = (ref.file, ref.name)
        prior_by_func.setdefault(key, []).append(m)

    # Build a lookup of prior function content hashes from FunctionRef on mutations
    prior_hash_by_func: dict[tuple[str, str], str] = {}
    for m in prior.mutations:
        if not m.function_refs:
            continue
        ref = m.function_refs[0]
        key = (ref.file, ref.name)
        if key not in prior_hash_by_func:
            prior_hash_by_func[key] = ref.content_hash

    # Classify current functions as changed or unchanged
    unchanged_func_keys: set[tuple[str, str]] = set()
    for func in current_functions:
        key = (func.ref.file, func.ref.name)
        prior_hash = prior_hash_by_func.get(key)
        if prior_hash is not None and prior_hash == func.ref.content_hash:
            unchanged_func_keys.add(key)

    # Split mutations
    mutations_to_run: list[Mutation] = []
    carried_over: list[Mutation] = []

    for m in current_mutations:
        if not m.function_refs:
            mutations_to_run.append(m)
            continue
        ref = m.function_refs[0]
        key = (ref.file, ref.name)
        if key in unchanged_func_keys:
            # Carry over the *prior* classified mutations for this function
            # (current pending mutations for unchanged functions are dropped)
            pass
        else:
            mutations_to_run.append(m)

    # Collect carried-over mutations from prior report (terminal statuses only).
    # Mutations with status=pending were never executed (e.g. agon was interrupted
    # mid-run) and must not be treated as classified results.
    _TERMINAL = {
        MutationStatus.killed,
        MutationStatus.survived,
        MutationStatus.equivalent,
        MutationStatus.timeout,
        MutationStatus.error,
    }
    for key in unchanged_func_keys:
        prior_mutations = prior_by_func.get(key, [])
        carried_over.extend(m for m in prior_mutations if m.status in _TERMINAL)

    # Functions to actually run through the sandbox
    functions_to_run = [
        f for f in current_functions
        if (f.ref.file, f.ref.name) not in unchanged_func_keys
    ]

    reused = len(carried_over)
    fresh = len(mutations_to_run)
    logger.info(
        "store: incremental filter — %d mutations reused from cache, %d to run",
        reused, fresh,
    )

    return functions_to_run, mutations_to_run, carried_over
