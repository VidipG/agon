"""
Jira specification source.

Fetches Jira ticket descriptions and converts them to SpecDocuments.

Configuration (via environment variables or .agon/config.toml):
  JIRA_URL        — base URL of your Jira instance (e.g. https://myorg.atlassian.net)
  JIRA_EMAIL      — Atlassian account email
  JIRA_API_TOKEN  — Atlassian API token (generate at id.atlassian.com/manage-profile/security)

Supports:
  kind="jira_ticket", location="PROJ-123"
  kind="jira_ticket", location="https://myorg.atlassian.net/browse/PROJ-123"
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone

from ..triggers.base import SpecRef
from .base import SpecDocument

_TICKET_ID_RE = re.compile(r"([A-Z][A-Z0-9]+-\d+)")


class JiraSpecSource:
    """Fetches Jira ticket descriptions via the Jira REST API v3."""

    def can_handle(self, ref: SpecRef) -> bool:
        return ref.kind == "jira_ticket"

    async def fetch(self, ref: SpecRef) -> SpecDocument:
        config = _JiraConfig.from_env()
        ticket_id = self._extract_ticket_id(ref.location)
        issue = await self._fetch_issue(config, ticket_id)
        return self._to_document(ref, ticket_id, issue)

    @staticmethod
    def _extract_ticket_id(location: str) -> str:
        match = _TICKET_ID_RE.search(location)
        if not match:
            raise ValueError(
                f"Could not extract a Jira ticket ID from {location!r}. "
                f"Expected format: 'PROJ-123' or a Jira browse URL."
            )
        return match.group(1)

    @staticmethod
    async def _fetch_issue(config: "_JiraConfig", ticket_id: str) -> dict:
        import httpx

        url = f"{config.base_url}/rest/api/3/issue/{ticket_id}"
        auth = httpx.BasicAuth(config.email, config.api_token)

        async with httpx.AsyncClient(auth=auth, timeout=30.0) as client:
            response = await client.get(url, params={"fields": "summary,description,issuetype,status,labels,comment"})
            if response.status_code == 404:
                raise ValueError(f"Jira ticket {ticket_id!r} not found. Check the ticket ID and your JIRA_URL.")
            if response.status_code == 401:
                raise PermissionError(
                    f"Jira authentication failed. Check JIRA_EMAIL and JIRA_API_TOKEN."
                )
            response.raise_for_status()

        return response.json()

    @staticmethod
    def _to_document(ref: SpecRef, ticket_id: str, issue: dict) -> SpecDocument:
        fields = issue.get("fields", {})
        summary = fields.get("summary", ticket_id)
        issue_type = fields.get("issuetype", {}).get("name", "Issue")
        status = fields.get("status", {}).get("name", "Unknown")
        labels = fields.get("labels", [])

        # Jira description is in Atlassian Document Format (ADF); extract plain text
        description_adf = fields.get("description")
        description_text = _adf_to_text(description_adf) if description_adf else ""

        # Include recent comments as additional context
        comments: list[dict] = fields.get("comment", {}).get("comments", [])
        comment_sections: list[str] = []
        for comment in comments[-5:]:   # last 5 comments
            author = comment.get("author", {}).get("displayName", "Unknown")
            body = _adf_to_text(comment.get("body"))
            if body:
                comment_sections.append(f"**{author}:** {body}")

        lines: list[str] = [
            f"# [{ticket_id}] {summary}",
            "",
            f"**Type:** {issue_type}  ",
            f"**Status:** {status}",
        ]
        if labels:
            lines.append(f"**Labels:** {', '.join(labels)}")
        if description_text:
            lines += ["", "## Description", "", description_text]
        if comment_sections:
            lines += ["", "## Comments", ""] + comment_sections

        return SpecDocument(
            source_ref=ref,
            title=f"[{ticket_id}] {summary}",
            content="\n".join(lines),
            format="jira_ticket",
            metadata={
                "ticket_id": ticket_id,
                "issue_type": issue_type,
                "status": status,
                "labels": labels,
            },
            fetched_at=datetime.now(timezone.utc),
        )


def _adf_to_text(node: dict | None) -> str:
    """Extract plain text from an Atlassian Document Format node (best-effort)."""
    if not node:
        return ""
    if isinstance(node, str):
        return node
    text_parts: list[str] = []
    node_type = node.get("type", "")
    if node_type == "text":
        return node.get("text", "")
    for child in node.get("content", []):
        child_text = _adf_to_text(child)
        if child_text:
            text_parts.append(child_text)
    separator = "\n" if node_type in ("paragraph", "heading", "listItem", "bulletList", "orderedList") else " "
    return separator.join(text_parts).strip()


class _JiraConfig:
    def __init__(self, base_url: str, email: str, api_token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.api_token = api_token

    @classmethod
    def from_env(cls) -> "_JiraConfig":
        base_url = os.environ.get("JIRA_URL", "").strip()
        email = os.environ.get("JIRA_EMAIL", "").strip()
        api_token = os.environ.get("JIRA_API_TOKEN", "").strip()

        missing = [name for name, val in [
            ("JIRA_URL", base_url),
            ("JIRA_EMAIL", email),
            ("JIRA_API_TOKEN", api_token),
        ] if not val]

        if missing:
            raise EnvironmentError(
                f"Missing required environment variables for Jira integration: {missing}. "
                f"Set them or configure jira.url / jira.email / jira.api_token in .agon/config.toml."
            )
        return cls(base_url, email, api_token)
