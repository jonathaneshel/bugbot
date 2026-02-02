from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from typing import Any

from .constants import BUGBOT_JIRA_LABEL, _JIRA_FIELDS_JSON_BEGIN, _JIRA_FIELDS_JSON_END
from .logging import _log
from .types import JiraIssue, RunnerOutput


def _parse_jira_instructions(raw: str) -> tuple[str, dict[str, Any]]:
    """
    Returns (prompt_instructions_text, config_dict).
    Config is optional; if absent/invalid, returns {}.
    """
    text = (raw or "").strip()
    if not text:
        return "", {}

    m = re.search(
        re.escape(_JIRA_FIELDS_JSON_BEGIN) + r"([\s\S]*?)" + re.escape(_JIRA_FIELDS_JSON_END),
        text,
    )
    if not m:
        return text, {}

    json_block = (m.group(1) or "").strip()
    prompt_text = (text[: m.start()] + "\n" + text[m.end() :]).strip()

    if not json_block:
        return prompt_text, {}

    try:
        cfg = json.loads(json_block)
        if isinstance(cfg, dict):
            return prompt_text, cfg
    except Exception:
        pass

    return prompt_text, {}


def _jira_basic_auth_header(email: str, api_token: str) -> str:
    raw = f"{email}:{api_token}".encode("utf-8")
    b64 = base64.b64encode(raw).decode("ascii")
    return f"Basic {b64}"


def _http_get_json(url: str, headers: dict[str, str]) -> Any:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


def update_jira_issue_fields(
    *,
    jira_base_url: str,
    issue_key: str,
    email: str,
    api_token: str,
    fields_payload: dict[str, Any],
) -> None:
    api_url = f"{jira_base_url.rstrip('/')}/rest/api/3/issue/{issue_key}"
    body = json.dumps({"fields": fields_payload}).encode("utf-8")

    req = urllib.request.Request(
        api_url,
        data=body,
        method="PUT",
        headers={
            "Authorization": _jira_basic_auth_header(email, api_token),
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            _ = resp.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"Jira update failed ({e.code}) for {api_url}. Details: {detail}") from e


def _adf_text(node: Any) -> str:
    """
    Best-effort conversion of Jira ADF (Atlassian Document Format) to plain text.
    Keeps it simple: collects 'text' nodes and adds newlines around paragraphs.
    """
    if node is None:
        return ""

    if isinstance(node, str):
        return node

    if isinstance(node, list):
        return "".join(_adf_text(x) for x in node)

    if isinstance(node, dict):
        node_type = node.get("type")
        if "text" in node and isinstance(node["text"], str):
            return node["text"]

        content = node.get("content")
        inner = _adf_text(content) if content is not None else ""

        if node_type in {"paragraph", "heading", "blockquote", "codeBlock"}:
            inner = inner.strip()
            return (inner + "\n\n") if inner else ""

        if node_type in {"listItem"}:
            inner = inner.strip()
            return (f"- {inner}\n") if inner else ""

        if node_type in {"bulletList", "orderedList"}:
            return inner + ("\n" if inner and not inner.endswith("\n") else "")

        return inner

    return ""


def fetch_jira_issue(
    *,
    jira_base_url: str,
    project_prefix: str,
    ticket_number: str,
    email: str,
    api_token: str,
) -> JiraIssue:
    key = f"{project_prefix}-{ticket_number}"
    browse_url = f"{jira_base_url.rstrip('/')}/browse/{key}"
    api_url = f"{jira_base_url.rstrip('/')}/rest/api/3/issue/{key}"

    headers = {
        "Accept": "application/json",
        "Authorization": _jira_basic_auth_header(email, api_token),
    }

    try:
        issue = _http_get_json(api_url, headers=headers)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"Jira API request failed ({e.code}) for {api_url}. Details: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Jira API request failed for {api_url}: {e}") from e

    fields = issue.get("fields", {}) if isinstance(issue, dict) else {}
    summary = str(fields.get("summary") or "").strip()
    issue_type = str((fields.get("issuetype") or {}).get("name") or "").strip()
    priority = str((fields.get("priority") or {}).get("name") or "").strip()
    description = fields.get("description")
    description_text = _adf_text(description).strip()

    return JiraIssue(
        key=key,
        url=browse_url,
        summary=summary,
        issue_type=issue_type,
        priority=priority,
        description_text=description_text,
    )


def ensure_bugbot_label(
    *,
    jira_base_url: str,
    issue_key: str,
    email: str,
    api_token: str,
) -> None:
    """
    Best-effort: ensure the Jira issue has the BugBot label.
    Matches the previous behavior: failures are swallowed and only a warning is logged.
    """
    try:
        raw_issue = _http_get_json(
            f"{jira_base_url.rstrip('/')}/rest/api/3/issue/{issue_key}",
            headers={
                "Authorization": _jira_basic_auth_header(email, api_token),
                "Accept": "application/json",
            },
        )
        raw_fields = raw_issue.get("fields", {}) if isinstance(raw_issue, dict) else {}
        labels = raw_fields.get("labels")
        if not isinstance(labels, list):
            labels = []
        cleaned = [str(x) for x in labels if isinstance(x, str) and x.strip()]
        has_bugbot = any(l.lower() == BUGBOT_JIRA_LABEL.lower() for l in cleaned)
        if not has_bugbot:
            new_labels = cleaned + [BUGBOT_JIRA_LABEL]
            _log(f"Adding Jira label {BUGBOT_JIRA_LABEL!r} to {issue_key}...")
            update_jira_issue_fields(
                jira_base_url=jira_base_url,
                issue_key=issue_key,
                email=email,
                api_token=api_token,
                fields_payload={"labels": new_labels},
            )
            _log("Jira label added.")
    except Exception as e:
        _log(f"WARNING: failed to ensure Jira label {BUGBOT_JIRA_LABEL!r}: {e}")


def _runner_output_value(ro: RunnerOutput, key: str) -> str:
    table = {
        "WHAT_DID_I_WORK_ON_DEV": ro.what_did_i_work_on_dev,
        "WHAT_DID_I_WORK_ON_TECH_PM": ro.what_did_i_work_on_tech_pm,
        "WHAT_DID_I_WORK_ON_NON_TECH_PM": ro.what_did_i_work_on_non_tech_pm,
        "WHAT_MIGHT_BE_IMPACTED": ro.what_might_be_impacted,
        "RCA": ro.rca,
        "RCA_COMMENTS": ro.rca_comments,
        "COMMIT_NAME": ro.commit_name,
    }
    if key not in table:
        raise RuntimeError(f"Unknown runner output key in JIRA_INSTRUCTIONS config: {key}")
    return table[key] or ""


def _format_for_jira(value: str, fmt: str, *, append_value: str = "") -> Any:
    """
    Returns a value suitable for Jira 'fields' payload.
    We keep it simple: send plain strings. (If your Jira fields require ADF, we can extend this.)
    """
    v = (value or "").strip()
    a = (append_value or "").strip()
    fmt = (fmt or "as_is").strip()

    if fmt == "bullets":
        # Input is usually "a; b; c" → "- a\n- b\n- c"
        parts = [p.strip() for p in v.split(";") if p.strip()]
        v = "\n".join([f"- {p}" for p in parts]) if parts else v

    if fmt == "rca_with_comments":
        if a:
            return f"{v}\n\n{a}".strip()
        return v

    # default: as-is (optionally append)
    return (v + ("\n" + a if a else "")).strip()


