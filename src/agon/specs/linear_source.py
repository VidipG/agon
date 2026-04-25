"""
Linear specification source.

Fetches Linear issue descriptions and converts them to SpecDocuments.

Configuration (via environment variable or .agon/config.toml):
  LINEAR_API_KEY — Personal API key from Linear settings → API → Personal API Keys

Supports:
  kind="linear_ticket", location="<issue-id>"         (e.g. "ENG-123" or UUID)
  kind="linear_ticket", location="https://linear.app/team/issue/ENG-123"
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone

from ..triggers.base import SpecRef
from .base import SpecDocument

_LINEAR_API_URL = "https://api.linear.app/graphql"

# Linear issue IDs: TEAM-123 or plain UUID
_TEAM_ID_RE = re.compile(r"([A-Z]+-\d+)")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

_ISSUE_QUERY = """
query GetIssue($id: String!) {
  issue(id: $id) {
    identifier
    title
    description
    state { name }
    team { name }
    labels { nodes { name } }
    comments { nodes { body user { name } } }
  }
}
"""


class LinearSpecSource:
    """Fetches Linear issue descriptions via the Linear GraphQL API."""

    def can_handle(self, ref: SpecRef) -> bool:
        return ref.kind == "linear_ticket"

    async def fetch(self, ref: SpecRef) -> SpecDocument:
        api_key = _LinearConfig.from_env().api_key
        issue_id = self._extract_issue_id(ref.location)
        issue = await self._fetch_issue(api_key, issue_id)
        return self._to_document(ref, issue)

    @staticmethod
    def _extract_issue_id(location: str) -> str:
        # Try TEAM-123 pattern first (most common in URLs and text)
        team_match = _TEAM_ID_RE.search(location)
        if team_match:
            return team_match.group(1)
        # Try UUID (used in Linear's internal API)
        uuid_match = _UUID_RE.search(location)
        if uuid_match:
            return uuid_match.group(0)
        raise ValueError(
            f"Could not extract a Linear issue ID from {location!r}. "
            f"Expected format: 'ENG-123', a UUID, or a linear.app URL."
        )

    @staticmethod
    async def _fetch_issue(api_key: str, issue_id: str) -> dict:
        import httpx

        headers = {
            "Authorization": api_key,
            "Content-Type": "application/json",
        }
        payload = {"query": _ISSUE_QUERY, "variables": {"id": issue_id}}

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(_LINEAR_API_URL, json=payload, headers=headers)
            if response.status_code == 401:
                raise PermissionError(
                    "Linear authentication failed. Check LINEAR_API_KEY."
                )
            response.raise_for_status()

        data = response.json()
        if errors := data.get("errors"):
            messages = "; ".join(e.get("message", str(e)) for e in errors)
            raise ValueError(f"Linear API error for issue {issue_id!r}: {messages}")

        issue = data.get("data", {}).get("issue")
        if not issue:
            raise ValueError(f"Linear issue {issue_id!r} not found.")
        return issue

    @staticmethod
    def _to_document(ref: SpecRef, issue: dict) -> SpecDocument:
        identifier = issue.get("identifier", "")
        title = issue.get("title", identifier)
        description = issue.get("description", "") or ""
        state = issue.get("state", {}).get("name", "Unknown")
        team = issue.get("team", {}).get("name", "")
        label_nodes = issue.get("labels", {}).get("nodes", [])
        labels = [lb["name"] for lb in label_nodes]

        comment_nodes = issue.get("comments", {}).get("nodes", [])
        comment_sections: list[str] = []
        for comment in comment_nodes[-5:]:  # last 5 comments
            author = comment.get("user", {}).get("name", "Unknown")
            body = comment.get("body", "").strip()
            if body:
                comment_sections.append(f"**{author}:** {body}")

        lines: list[str] = [
            f"# [{identifier}] {title}",
            "",
        ]
        if team:
            lines.append(f"**Team:** {team}  ")
        lines.append(f"**Status:** {state}")
        if labels:
            lines.append(f"**Labels:** {', '.join(labels)}")
        if description:
            lines += ["", "## Description", "", description]
        if comment_sections:
            lines += ["", "## Comments", ""] + comment_sections

        return SpecDocument(
            source_ref=ref,
            title=f"[{identifier}] {title}",
            content="\n".join(lines),
            format="linear_ticket",
            metadata={
                "identifier": identifier,
                "state": state,
                "team": team,
                "labels": labels,
            },
            fetched_at=datetime.now(timezone.utc),
        )


class _LinearConfig:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    @classmethod
    def from_env(cls) -> "_LinearConfig":
        api_key = os.environ.get("LINEAR_API_KEY", "").strip()
        if not api_key:
            raise EnvironmentError(
                "Missing required environment variable LINEAR_API_KEY for Linear integration. "
                "Generate one at Linear → Settings → API → Personal API Keys."
            )
        return cls(api_key)
