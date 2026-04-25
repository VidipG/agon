"""
SpecRef resolution: raw --spec strings → SpecRef instances.

Design: URI prefix convention + resolver registry.

  --spec jira:PROJ-123             → SpecRef(kind="jira_ticket", ...)
  --spec linear:ENG-456            → SpecRef(kind="linear_ticket", ...)
  --spec openapi:./api.yaml        → SpecRef(kind="openapi_file", ...)
  --spec openapi:https://api.../   → SpecRef(kind="openapi_url", ...)
  --spec ./requirements.md         → SpecRef(kind="file", ...)        (filesystem)
  --spec https://docs.example.com  → SpecRef(kind="url", ...)         (URL)

Extensibility
─────────────
Adding a new spec source type requires exactly two things:
  1. Write a SpecRefResolver subclass.
  2. Call registry.register(MyNewResolver()).

Nothing else needs to change. The parsing logic in SpecRefRegistry is
source-type-agnostic — it dispatches by prefix or delegates to can_resolve().

Resolver protocol
─────────────────
Each resolver declares:
  prefix       — the URI prefix it owns ("jira", "linear", None for bare-string)
  can_resolve  — whether it accepts a bare (prefix-free) string
  resolve      — constructs the SpecRef from the location string

Prefix resolvers may also implement can_resolve() to handle short-form bare
strings as a convenience (e.g. JiraResolver accepts bare "PROJ-123").
Filesystem and URL resolvers have no prefix and rely solely on can_resolve().
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar, Protocol, runtime_checkable

from .base import SpecRef

_JIRA_ID_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
_GH_SHORTFORM_RE = re.compile(r"^[^/]+/[^#]+#\d+$")


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SpecRefResolver(Protocol):
    """Resolves raw --spec strings into SpecRef instances.

    Implementations must be stateless and safe to call concurrently.
    """

    prefix: ClassVar[str | None]
    """URI prefix this resolver owns (e.g. "jira", "linear").
    None for resolvers that handle bare strings without a prefix."""

    def can_resolve(self, raw: str) -> bool:
        """Return True if this resolver can handle the raw string.

        Called during bare-string dispatch (no prefix present).
        Prefix resolvers may also implement this for short-form convenience.
        """
        ...

    def resolve(self, raw: str) -> SpecRef:
        """Construct a SpecRef from the raw location string.

        For prefix resolvers, raw has the prefix stripped.
        For bare-string resolvers, raw is the original string.
        """
        ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class SpecRefRegistry:
    """Dispatches raw --spec strings to the appropriate SpecRefResolver.

    Dispatch order:
    1. If raw contains ":" and is not a URL → prefix dispatch.
       The prefix before ":" selects the resolver. Unknown prefix → ValueError
       with a list of known prefixes (no silent fallback).
    2. Otherwise → bare-string dispatch.
       Resolvers are tried in registration order; first can_resolve() wins.
       No match → ValueError with actionable suggestions.

    Usage
    ─────
    registry = SpecRefRegistry.default()
    ref = registry.resolve("jira:PROJ-123")   # → SpecRef(kind="jira_ticket")
    ref = registry.resolve("./api.yaml")      # → SpecRef(kind="openapi_file")

    Extending
    ─────────
    registry.register(NotionResolver())  # prefix="notion"
    ref = registry.resolve("notion:abc-123-def")
    """

    def __init__(self) -> None:
        self._resolvers: list[SpecRefResolver] = []

    def register(self, resolver: SpecRefResolver) -> None:
        self._resolvers.append(resolver)

    def resolve(self, raw: str) -> SpecRef:
        raw = raw.strip()
        if not raw:
            raise ValueError("Empty spec source string.")

        # --- Prefix dispatch ---
        # A colon not at position 1-2 (drive letters on Windows) that is not
        # preceded by "http" or "https" signals an explicit prefix.
        if _is_prefixed(raw):
            prefix, _, location = raw.partition(":")
            prefix = prefix.lower()
            for resolver in self._resolvers:
                if resolver.prefix == prefix:
                    return resolver.resolve(location)
            known = sorted(r.prefix for r in self._resolvers if r.prefix)
            raise ValueError(
                f"Unknown spec prefix {prefix!r}.\n"
                f"Available prefixes: {known}.\n"
                f"Example: jira:PROJ-123, openapi:./api.yaml"
            )

        # --- Bare-string dispatch ---
        for resolver in self._resolvers:
            if resolver.can_resolve(raw):
                return resolver.resolve(raw)

        raise ValueError(
            f"Cannot resolve spec source {raw!r}.\n"
            f"For ticket IDs, use a prefix:  jira:PROJ-123  or  linear:ENG-456\n"
            f"For files, pass a path:         ./requirements.md  or  ./specs/\n"
            f"For OpenAPI:                    openapi:./api.yaml  or  openapi:https://...\n"
            f"For arbitrary URLs:             https://docs.example.com/spec"
        )

    @classmethod
    def default(cls) -> SpecRefRegistry:
        """Create a registry with all built-in resolvers pre-registered.

        Registration order matters for bare-string dispatch: more specific
        resolvers should come before more general ones.
        """
        registry = cls()
        registry.register(URLResolver())           # bare: http(s):// URLs
        registry.register(FilesystemResolver())    # bare: existing filesystem paths
        registry.register(JiraResolver())          # prefix "jira" + bare PROJ-123
        registry.register(LinearResolver())        # prefix "linear"
        registry.register(GitHubIssueResolver())   # prefix "gh" + bare owner/repo#N
        registry.register(OpenAPIResolver())       # prefix "openapi"
        registry.register(FileResolver())          # prefix "file" (explicit override)
        return registry


def _is_prefixed(raw: str) -> bool:
    """Return True if raw starts with an explicit spec prefix.

    Excludes http/https (they contain colons but are not prefixes),
    and Windows drive letters like C: (single-char prefix).
    """
    if raw.startswith(("http://", "https://")):
        return False
    idx = raw.find(":")
    if idx <= 1:        # no colon, or single-char "prefix" (Windows drive letter)
        return False
    return True


# ---------------------------------------------------------------------------
# Built-in resolvers
# ---------------------------------------------------------------------------


class URLResolver:
    """Bare-string resolver for http(s):// URLs.

    Detects OpenAPI specs by URL keyword/extension; everything else is "url".
    Use 'openapi:https://...' to be explicit.
    """

    prefix: ClassVar[str | None] = None

    def can_resolve(self, raw: str) -> bool:
        return raw.startswith(("http://", "https://"))

    def resolve(self, raw: str) -> SpecRef:
        lower = raw.lower()
        if (
            "openapi" in lower
            or "swagger" in lower
            or lower.split("?")[0].endswith((".yaml", ".yml", ".json"))
        ):
            return SpecRef(kind="openapi_url", location=raw)
        return SpecRef(kind="url", location=raw)


class FilesystemResolver:
    """Bare-string resolver for paths that exist on the local filesystem.

    Filesystem existence is checked before any pattern matching, preventing
    files named like ticket IDs (e.g. PROJ-123.md) from being misidentified.
    """

    prefix: ClassVar[str | None] = None

    def can_resolve(self, raw: str) -> bool:
        return Path(raw).exists()

    def resolve(self, raw: str) -> SpecRef:
        path = Path(raw)
        if path.is_dir():
            return SpecRef(kind="directory", location=raw)
        return SpecRef(kind="file", location=raw)


class JiraResolver:
    """Handles Jira ticket references.

    Prefix form:  jira:PROJ-123  or  jira:https://org.atlassian.net/browse/PROJ-123
    Bare form:    PROJ-123  (convenience — only when not a filesystem path)
    """

    prefix: ClassVar[str | None] = "jira"

    def can_resolve(self, raw: str) -> bool:
        return bool(_JIRA_ID_RE.match(raw))

    def resolve(self, raw: str) -> SpecRef:
        return SpecRef(kind="jira_ticket", location=raw)


class LinearResolver:
    """Handles Linear issue references.

    Prefix form:  linear:ENG-456  or  linear:https://linear.app/team/issue/ENG-456
    No bare-string convenience (Linear IDs are not as distinctive as PROJ-123).
    """

    prefix: ClassVar[str | None] = "linear"

    def can_resolve(self, raw: str) -> bool:
        return False

    def resolve(self, raw: str) -> SpecRef:
        return SpecRef(kind="linear_ticket", location=raw)


class GitHubIssueResolver:
    """Handles GitHub issue references.

    Prefix form:  gh:owner/repo#42
    Bare form:    owner/repo#42  (distinctive enough to be a convenience)
    """

    prefix: ClassVar[str | None] = "gh"

    def can_resolve(self, raw: str) -> bool:
        return bool(_GH_SHORTFORM_RE.match(raw))

    def resolve(self, raw: str) -> SpecRef:
        return SpecRef(kind="github_issue", location=raw)


class OpenAPIResolver:
    """Handles OpenAPI / Swagger specifications with an explicit prefix.

    Prefix form:
      openapi:./api.yaml              → openapi_file
      openapi:https://api.../spec     → openapi_url

    Explicit prefix is preferred over URL auto-detection for OpenAPI.
    """

    prefix: ClassVar[str | None] = "openapi"

    def can_resolve(self, raw: str) -> bool:
        return False

    def resolve(self, raw: str) -> SpecRef:
        if raw.startswith(("http://", "https://")):
            return SpecRef(kind="openapi_url", location=raw)
        return SpecRef(kind="openapi_file", location=raw)


class FileResolver:
    """Explicit 'file:' prefix to force file treatment.

    Useful when a path is ambiguous or does not yet exist on disk.
    Example:  file:./future-spec.md
    """

    prefix: ClassVar[str | None] = "file"

    def can_resolve(self, raw: str) -> bool:
        return False

    def resolve(self, raw: str) -> SpecRef:
        return SpecRef(kind="file", location=raw)
