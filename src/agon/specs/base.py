"""
Specification input protocol.

SpecSource implementations know how to fetch and parse specifications from
a particular source type (files, OpenAPI, Jira, Linear, etc).

The SpecLoader dispatches SpecRef instances to the right SpecSource and
returns SpecDocument objects ready for eigentest's spec_extractor to consume.

Adding a new source type:
  1. Implement SpecSource (can_handle + fetch).
  2. Register it: SpecLoader.default() or SpecLoader.register().
  No changes needed elsewhere.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from ..triggers.base import SpecRef


class SpecDocument(BaseModel):
    """A parsed specification document ready for eigentest consumption.

    content     — raw text (markdown, plain text, ticket description)
    format      — controls how eigentest's spec_extractor processes it
    structured_data — for OpenAPI specs: the fully parsed dict object,
                      allowing mechanical extraction of endpoint contracts
                      without re-parsing the YAML/JSON
    """

    source_ref: SpecRef
    title: str
    content: str            # raw text; always populated
    format: Literal[
        "markdown",
        "openapi",
        "plaintext",
        "jira_ticket",
        "linear_ticket",
        "github_issue",
    ]
    structured_data: dict[str, Any] | None = None   # parsed object (OpenAPI only in v1)
    metadata: dict[str, Any] = {}
    fetched_at: datetime | None = None


@runtime_checkable
class SpecSource(Protocol):
    """Fetch and parse specs from a given source type.

    Implementations must be safe to call concurrently — SpecLoader
    may invoke fetch() on multiple refs in parallel.
    """

    def can_handle(self, ref: SpecRef) -> bool:
        """Return True if this source knows how to handle the given SpecRef."""
        ...

    async def fetch(self, ref: SpecRef) -> SpecDocument:
        """Fetch and parse the spec referenced by ref."""
        ...


class SpecLoader:
    """Dispatches SpecRef instances to the appropriate SpecSource.

    Sources are tried in registration order; the first that returns
    True from can_handle() wins.
    """

    def __init__(
        self,
        sources: list[SpecSource] | None = None,
        max_concurrent: int = 10,
    ) -> None:
        self._sources: list[SpecSource] = list(sources or [])
        self._max_concurrent = max_concurrent

    def register(self, source: SpecSource) -> None:
        self._sources.append(source)

    async def load(self, ref: SpecRef) -> SpecDocument:
        for source in self._sources:
            if source.can_handle(ref):
                return await source.fetch(ref)
        raise ValueError(
            f"No SpecSource registered for kind={ref.kind!r} location={ref.location!r}. "
            f"Registered sources: {[type(s).__name__ for s in self._sources]}"
        )

    async def load_all(self, refs: list[SpecRef]) -> list[SpecDocument]:
        """Fetch all spec refs concurrently, bounded by max_concurrent.

        The concurrency cap prevents hammering rate limits when loading many
        remote sources (Jira, Linear, OpenAPI URLs) simultaneously.
        Default: 10 concurrent fetches.
        """
        if not refs:
            return []

        import anyio

        results: list[SpecDocument | None] = [None] * len(refs)
        errors: list[Exception | None] = [None] * len(refs)
        semaphore = anyio.Semaphore(self._max_concurrent)

        async def _fetch(i: int, ref: SpecRef) -> None:
            async with semaphore:
                try:
                    results[i] = await self.load(ref)
                except Exception as exc:  # noqa: BLE001
                    errors[i] = exc

        async with anyio.create_task_group() as tg:
            for i, ref in enumerate(refs):
                tg.start_soon(_fetch, i, ref)

        first_error = next((e for e in errors if e is not None), None)
        if first_error:
            raise first_error

        return results  # type: ignore[return-value]

    @classmethod
    def default(cls) -> SpecLoader:
        """Create a SpecLoader with all built-in sources registered."""
        from .file_source import FileSpecSource
        from .openapi_source import OpenAPISpecSource
        from .jira_source import JiraSpecSource
        from .linear_source import LinearSpecSource

        return cls(sources=[
            FileSpecSource(),
            OpenAPISpecSource(),
            JiraSpecSource(),
            LinearSpecSource(),
        ])
