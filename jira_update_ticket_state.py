#!/usr/bin/env python3
"""
Jira state normalizer for Labguru tickets.

Rules (hardcoded per team workflow):
- Refinement Status: ensure "Refined"
- Original estimate: if empty, set to "5m"
- If status is Pending -> transition via "Ready & Approved" (moves to TODO)
- If status is TODO/To Do -> transition via "Start Work" (moves to In Progress)

Auth (env vars):
  - JIRA_EMAIL
  - JIRA_API_TOKEN
Optional:
  - JIRA_BASE_URL (default https://labguru.atlassian.net)

Usage:
  python3 jira_update_ticket_state.py LAB-30353
  python3 jira_update_ticket_state.py 30353
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any, Optional


DEFAULT_JIRA_BASE_URL = "https://labguru.atlassian.net"
DEFAULT_PROJECT_PREFIX = "LAB"

REFINEMENT_FIELD_NAME = "Refinement status"
REFINEMENT_TARGET_VALUE = "Refined"
DEFAULT_ORIGINAL_ESTIMATE = "5m"

TRANSITION_PENDING_TO_TODO = "Ready & Approved"
TRANSITION_TODO_TO_IN_PROGRESS = "Start Work"
TRANSITION_IN_PROGRESS_TO_READY_FOR_REVIEW = "Ready For Review"

FIELD_WHAT_DID_I_WORK_ON = "What did I work on"
FIELD_WHAT_MIGHT_BE_IMPACTED = "What might be impacted"
FIELD_CLASSIFICATION = "Classification"
FIELD_SERVICE = "Service"
FIELD_BUG_IMPACT = "Bug impact"
FIELD_DEVELOPER_RCA_CANDIDATE_NAMES = (
    "Developer RCA",
    "Root cause analysis",
    "Root Cause Analysis",
)
CLASSIFICATION_VALUE = "Code change"
SERVICE_VALUE = "Labguru"
BUG_IMPACT_VALUE = "Major"
DEVELOPER_RCA_DEFAULT_VALUE = "Other"
BUGBOT_COMMENT_TEXT = "made with BugBot"
BUGBOT_LABEL = "BugBot"

DEFAULT_CURSOR_BIN = os.getenv("CURSOR_BIN", "cursor-agent")
DEFAULT_CURSOR_TIMEOUT_SECONDS = int(os.getenv("CURSOR_TIMEOUT_SECONDS", "180"))
DEFAULT_CURSOR_RETRIES = int(os.getenv("CURSOR_RETRIES", "2"))
DEFAULT_CURSOR_RETRY_BACKOFF_SECONDS = float(os.getenv("CURSOR_RETRY_BACKOFF_SECONDS", "1.5"))
DEFAULT_CURSOR_LOG_FILE = os.getenv(
    "CURSOR_LOG_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "last_cursor_agent_jira_updater.log"),
)
DEFAULT_RUNNER_OUTPUT_JSON = os.getenv("RUNNER_OUTPUT_JSON", "")


def _load_runner_output_json(path: str) -> dict[str, Any]:
    p = os.path.abspath(os.path.expanduser(path))
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("runner output JSON must be an object")
    return data


def _die(msg: str) -> int:
    print(msg, file=sys.stderr)
    return 2


def _extract_ticket_number(raw: str) -> str:
    m = re.search(r"\b(\d+)\b", raw or "")
    if not m:
        raise ValueError(f"Could not find a ticket number in: {raw!r}")
    return m.group(1)


def _issue_key_from_input(raw: str, project_prefix: str) -> str:
    raw = (raw or "").strip()
    m = re.search(rf"\b{re.escape(project_prefix)}-(\d+)\b", raw, flags=re.IGNORECASE)
    if m:
        return f"{project_prefix}-{m.group(1)}"
    return f"{project_prefix}-{_extract_ticket_number(raw)}"


def _jira_basic_auth_header(email: str, api_token: str) -> str:
    raw = f"{email}:{api_token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _http_json(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    body: Optional[dict[str, Any]] = None,
) -> Any:
    data = None
    req_headers = dict(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, headers=req_headers, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            if not raw.strip():
                return None
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"Jira API failed ({e.code}) {method} {url}: {detail}") from e


def _ensure_bugbot_label(
    *,
    base: str,
    issue_key: str,
    headers: dict[str, str],
    dry_run: bool,
) -> None:
    """
    Ensure the Jira issue has the 'BugBot' label.
    Best-effort: warnings only (do not block normalizer flow).
    """
    try:
        issue_url = f"{base}/rest/api/3/issue/{issue_key}"
        issue = _http_json(method="GET", url=issue_url, headers=headers)
        fields = issue.get("fields", {}) if isinstance(issue, dict) else {}
        labels = fields.get("labels")
        if not isinstance(labels, list):
            labels = []
        cleaned = [str(x) for x in labels if isinstance(x, str) and x.strip()]
        has_bugbot = any(l.lower() == BUGBOT_LABEL.lower() for l in cleaned)
        if has_bugbot:
            return
        new_labels = cleaned + [BUGBOT_LABEL]
        if dry_run:
            print(f"[dry-run] Would add label {BUGBOT_LABEL!r} to {issue_key}")
            return
        _http_json(method="PUT", url=issue_url, headers=headers, body={"fields": {"labels": new_labels}})
        print(f"[jira_update_ticket_state] Added label {BUGBOT_LABEL!r} to {issue_key}", file=sys.stderr)
    except Exception as e:
        print(
            f"[jira_update_ticket_state] WARNING: failed to ensure label {BUGBOT_LABEL!r}: {e}",
            file=sys.stderr,
        )


def _ensure_assigned_to_me(
    *,
    base: str,
    issue_key: str,
    headers: dict[str, str],
    dry_run: bool,
) -> None:
    """
    Ensure the Jira issue is assigned to the current API user (\"me\").
    Best-effort: warnings only (do not block normalizer flow).

    Jira Cloud requires assignee by `accountId`, so we fetch it from /myself.
    """
    try:
        me = _http_json(method="GET", url=f"{base}/rest/api/3/myself", headers=headers)
        account_id = str((me or {}).get("accountId") or "").strip() if isinstance(me, dict) else ""
        if not account_id:
            return
        if dry_run:
            print(f"[dry-run] Would assign {issue_key} to current user (accountId={account_id})")
            return
        _http_json(
            method="PUT",
            url=f"{base}/rest/api/3/issue/{issue_key}/assignee",
            headers=headers,
            body={"accountId": account_id},
        )
        print(f"[jira_update_ticket_state] Assigned {issue_key} to current user", file=sys.stderr)
    except Exception as e:
        print(
            f"[jira_update_ticket_state] WARNING: failed to ensure assignee is current user: {e}",
            file=sys.stderr,
        )


def _adf_doc(text: str) -> dict[str, Any]:
    # Jira Cloud rich-text fields/comments use Atlassian Document Format (ADF).
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}],
            }
        ],
    }


def _adf_text(node: Any) -> str:
    # Best-effort ADF -> plain text
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf_text(x) for x in node)
    if isinstance(node, dict):
        if "text" in node and isinstance(node["text"], str):
            return node["text"]
        node_type = node.get("type")
        inner = _adf_text(node.get("content"))
        if node_type in {"paragraph", "heading"}:
            inner = inner.strip()
            return (inner + "\n\n") if inner else ""
        return inner
    return ""


def _find_field_id(fields: list[dict[str, Any]], *, field_name: str) -> Optional[str]:
    want = field_name.strip().lower()
    for f in fields:
        if str(f.get("name") or "").strip().lower() == want:
            fid = str(f.get("id") or "").strip()
            return fid or None
    return None


def _find_first_field_id(
    fields: list[dict[str, Any]], *, field_names: tuple[str, ...]
) -> Optional[str]:
    for name in field_names:
        fid = _find_field_id(fields, field_name=name)
        if fid:
            return fid
    return None


def _is_empty_value(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, str):
        return not val.strip()
    if isinstance(val, dict):
        # option/ADF cases
        if "value" in val or "name" in val:
            return not str(val.get("value") or val.get("name") or "").strip()
        if val.get("type") == "doc":
            return not _adf_text(val).strip()
        return not bool(val)
    if isinstance(val, list):
        return len(val) == 0
    return False


def _pick_allowed_option(allowed_values: list[dict[str, Any]], desired: str) -> Optional[dict[str, Any]]:
    want_raw = desired.strip()
    want = want_raw.lower()

    def norm(s: str) -> str:
        out = []
        last_space = False
        for ch in s.lower():
            if ch.isalnum():
                out.append(ch)
                last_space = False
            else:
                if not last_space:
                    out.append(" ")
                    last_space = True
        return " ".join("".join(out).split())

    for opt in allowed_values:
        label = (opt.get("value") or opt.get("name") or "").strip()
        if label.lower() == want:
            return opt

    want_n = norm(want_raw)
    matches: list[dict[str, Any]] = []
    for opt in allowed_values:
        label = (opt.get("value") or opt.get("name") or "").strip()
        if not label:
            continue
        label_n = norm(label)
        if want_n and (want_n in label_n or label_n in want_n):
            matches.append(opt)
    if len(matches) == 1:
        return matches[0]
    return None


def _apply_option_payload_schema(field_meta: dict[str, Any], option_payload: dict[str, Any]) -> Any:
    schema = field_meta.get("schema") or {}
    schema_type = str(schema.get("type") or "").strip().lower()
    items = str(schema.get("items") or "").strip().lower()
    if schema_type == "array" and items in {"option", "any"}:
        return [option_payload]
    return option_payload


def _cursor_generate_work_and_impact(*, issue_key: str, summary: str, description_text: str) -> tuple[str, str]:
    """
    Ask cursor-agent (headlessly) to propose:
      - WHAT_DID_I_WORK_ON
      - WHAT_MIGHT_BE_IMPACTED
    """
    prompt = (
        "You are a PM-friendly summarizer.\n"
        "Given this Jira ticket, propose concise field values.\n"
        "Do NOT ask questions.\n"
        "Output EXACTLY two lines and nothing else:\n"
        "WHAT_DID_I_WORK_ON: <one line>\n"
        "WHAT_MIGHT_BE_IMPACTED: <one line>\n\n"
        f"Issue: {issue_key}\n"
        f"Summary: {summary}\n"
        "Description:\n"
        f"{description_text.strip() or '(empty)'}\n"
    )

    cmd = [DEFAULT_CURSOR_BIN]
    attempts_total = max(1, int(DEFAULT_CURSOR_RETRIES) + 1)
    last_err = ""
    last_out = ""
    last_stderr = ""

    for attempt in range(1, attempts_total + 1):
        res = subprocess.run(
            cmd,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=DEFAULT_CURSOR_TIMEOUT_SECONDS,
        )
        last_out = (res.stdout or "").strip()
        last_stderr = (res.stderr or "").strip()
        if res.returncode == 0:
            break

        last_err = last_stderr or "cursor-agent failed"
        # Retry only for common transient Cursor CLI error.
        if "connection stalled" in last_err.lower() and attempt < attempts_total:
            sleep_s = DEFAULT_CURSOR_RETRY_BACKOFF_SECONDS * attempt
            print(
                f"[jira_update_ticket_state] cursor-agent 'Connection stalled' (attempt {attempt}/{attempts_total}); retrying in {sleep_s:.1f}s...",
                file=sys.stderr,
            )
            try:
                import time

                time.sleep(sleep_s)
            except Exception:
                pass
            continue
        break

    # Always write the last attempt output to a local log file for debugging.
    try:
        log_path = os.path.abspath(os.path.expanduser(DEFAULT_CURSOR_LOG_FILE))
        parent = os.path.dirname(log_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(log_path, "wb") as f:
            f.write((last_out or "").encode("utf-8", errors="replace"))
            if last_stderr:
                f.write(b"\n\n--- STDERR ---\n")
                f.write(last_stderr.encode("utf-8", errors="replace"))
        print(f"[jira_update_ticket_state] Wrote cursor-agent log: {log_path}", file=sys.stderr)
    except Exception:
        pass

    out = last_out
    if res.returncode != 0:
        raise RuntimeError(f"cursor-agent failed: {last_err}")

    what = ""
    impacted = ""
    for line in out.splitlines():
        if line.startswith("WHAT_DID_I_WORK_ON:"):
            what = line.split(":", 1)[1].strip()
        if line.startswith("WHAT_MIGHT_BE_IMPACTED:"):
            impacted = line.split(":", 1)[1].strip()
    if not what or not impacted:
        raise RuntimeError(f"cursor-agent output missing required lines. Got:\n{out}")
    return what, impacted


def _is_refined(current_val: Any) -> bool:
    """
    current_val can be:
      - {"id": "...", "value": "Refined"} (single)
      - [{"value": "Refined"}] (multi)
      - None
    """
    target = REFINEMENT_TARGET_VALUE.lower()
    if current_val is None:
        return False

    if isinstance(current_val, dict):
        label = str(current_val.get("value") or current_val.get("name") or "").strip().lower()
        return label == target

    if isinstance(current_val, list):
        for item in current_val:
            if not isinstance(item, dict):
                continue
            label = str(item.get("value") or item.get("name") or "").strip().lower()
            if label == target:
                return True
        return False

    return False


def _pick_transition_by_name(transitions: list[dict[str, Any]], name: str) -> Optional[str]:
    want = name.strip().lower()
    for t in transitions:
        tname = str(t.get("name") or "").strip().lower()
        if tname == want:
            tid = str(t.get("id") or "").strip()
            return tid or None
    return None


def _list_transition_names(transitions: list[dict[str, Any]]) -> str:
    return ", ".join(str(t.get("name") or "") for t in transitions[:25])


def _needs_original_estimate(timetracking: Any) -> bool:
    """
    Treat "empty" estimates as needing a default:
    - missing timetracking object
    - originalEstimate missing/blank
    - originalEstimateSeconds == 0
    - originalEstimate like "0m"
    """
    if not isinstance(timetracking, dict):
        return True

    seconds = timetracking.get("originalEstimateSeconds")
    if isinstance(seconds, int) and seconds <= 0:
        return True

    est = str(timetracking.get("originalEstimate") or "").strip()
    if not est:
        return True
    if est.lower() in {"0m", "0"}:
        return True
    return False


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Normalize Jira ticket state for Labguru workflow.")
    parser.add_argument("ticket", help="e.g. 30353 or LAB-30353")
    parser.add_argument("--project-prefix", default=DEFAULT_PROJECT_PREFIX)
    parser.add_argument(
        "--jira-base-url",
        default=os.environ.get("JIRA_BASE_URL", DEFAULT_JIRA_BASE_URL),
    )
    parser.add_argument(
        "--runner-output-json",
        default=DEFAULT_RUNNER_OUTPUT_JSON,
        help="Optional path to runner output JSON (from ticket_runner.py). When provided, reuse it to fill empty fields without calling cursor-agent.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not mutate Jira. Only GET + print the actions/payloads that would be applied.",
    )
    args = parser.parse_args(argv)

    email = os.environ.get("JIRA_EMAIL", "").strip()
    token = os.environ.get("JIRA_API_TOKEN", "").strip()
    if not email or not token:
        return _die("Missing Jira credentials. Set env vars: JIRA_EMAIL and JIRA_API_TOKEN")

    base = args.jira_base_url.rstrip("/")
    issue_key = _issue_key_from_input(args.ticket, args.project_prefix.strip().upper())

    runner_json: dict[str, Any] = {}
    if str(args.runner_output_json or "").strip():
        try:
            runner_json = _load_runner_output_json(args.runner_output_json)
        except Exception as e:
            print(
                f"WARNING: could not load --runner-output-json ({args.runner_output_json}): {e}",
                file=sys.stderr,
            )
            runner_json = {}

    headers = {
        "Accept": "application/json",
        "Authorization": _jira_basic_auth_header(email, token),
    }

    _ensure_bugbot_label(base=base, issue_key=issue_key, headers=headers, dry_run=bool(args.dry_run))
    _ensure_assigned_to_me(base=base, issue_key=issue_key, headers=headers, dry_run=bool(args.dry_run))

    # Field discovery
    fields = _http_json(method="GET", url=f"{base}/rest/api/3/field", headers=headers)
    if not isinstance(fields, list):
        return _die("Unexpected Jira /field response (expected list).")

    refinement_field_id = _find_field_id(fields, field_name=REFINEMENT_FIELD_NAME)
    if not refinement_field_id:
        return _die(f"Could not find Jira field named exactly {REFINEMENT_FIELD_NAME!r}.")

    # Fetch issue
    issue = _http_json(method="GET", url=f"{base}/rest/api/3/issue/{issue_key}", headers=headers)
    if not isinstance(issue, dict):
        return _die("Unexpected Jira issue response.")
    issue_fields = issue.get("fields", {}) if isinstance(issue.get("fields"), dict) else {}
    summary = str(issue_fields.get("summary") or "").strip()
    description_text = _adf_text(issue_fields.get("description")).strip()

    status_name = ""
    try:
        status_name = str(((issue_fields.get("status") or {}) or {}).get("name") or "").strip()
    except Exception:
        status_name = ""

    timetracking = issue_fields.get("timetracking") if isinstance(issue_fields.get("timetracking"), dict) else {}

    current_refinement_val = issue_fields.get(refinement_field_id)

    # editmeta for refinement allowed values + schema
    editmeta = _http_json(
        method="GET",
        url=f"{base}/rest/api/3/issue/{issue_key}/editmeta",
        headers=headers,
    )
    edit_fields = (editmeta or {}).get("fields", {}) if isinstance(editmeta, dict) else {}
    refinement_meta = edit_fields.get(refinement_field_id, {}) if isinstance(edit_fields, dict) else {}
    allowed_values = refinement_meta.get("allowedValues") or []
    if not isinstance(allowed_values, list) or not allowed_values:
        return _die(
            f"No allowedValues for {REFINEMENT_FIELD_NAME!r} ({refinement_field_id}). "
            "You may not have permission to edit it, or the field isn't editable on this issue."
        )

    updates: dict[str, Any] = {}
    did_set_estimate = False

    if not _is_refined(current_refinement_val):
        opt = _pick_allowed_option(allowed_values, REFINEMENT_TARGET_VALUE)
        if not opt:
            sample = ", ".join(str(o.get("value") or o.get("name") or "") for o in allowed_values[:15])
            return _die(f"Could not find refinement option {REFINEMENT_TARGET_VALUE!r}. First allowed: {sample}")
        if opt.get("id"):
            payload_raw = {"id": str(opt["id"])}
        else:
            payload_raw = {"value": str(opt.get("value") or opt.get("name") or "")}
        updates[refinement_field_id] = _apply_option_payload_schema(refinement_meta, payload_raw)

    if _needs_original_estimate(timetracking):
        updates["timetracking"] = {"originalEstimate": DEFAULT_ORIGINAL_ESTIMATE}
        did_set_estimate = True

    if updates:
        if args.dry_run:
            print(f"[dry-run] Would update fields on {issue_key}: {json.dumps(updates, ensure_ascii=False)}")
        else:
            _http_json(
                method="PUT",
                url=f"{base}/rest/api/3/issue/{issue_key}",
                headers=headers,
                body={"fields": updates},
            )
        # Re-fetch status for transition logic (status can change via automation after edits)
        issue = _http_json(method="GET", url=f"{base}/rest/api/3/issue/{issue_key}", headers=headers)
        issue_fields = issue.get("fields", {}) if isinstance(issue.get("fields"), dict) else {}
        status_name = str(((issue_fields.get("status") or {}) or {}).get("name") or "").strip()

    # Transitions
    def fetch_transitions() -> list[dict[str, Any]]:
        trans = _http_json(
            method="GET",
            url=f"{base}/rest/api/3/issue/{issue_key}/transitions",
            headers=headers,
        )
        transitions = (trans or {}).get("transitions", []) if isinstance(trans, dict) else []
        return transitions if isinstance(transitions, list) else []

    def do_transition(action_name: str, *, transition_fields: Optional[dict[str, Any]] = None) -> None:
        transitions = fetch_transitions()
        tid = _pick_transition_by_name(transitions, action_name)
        if not tid:
            raise RuntimeError(
                f"Could not find transition {action_name!r} for {issue_key}. First transitions: {_list_transition_names(transitions)}"
            )
        body: dict[str, Any] = {"transition": {"id": tid}}
        if transition_fields:
            body["fields"] = transition_fields
        if args.dry_run:
            print(f"[dry-run] Would transition {issue_key} via {action_name!r}: {json.dumps(body, ensure_ascii=False)}")
            return
        _http_json(
            method="POST",
            url=f"{base}/rest/api/3/issue/{issue_key}/transitions",
            headers=headers,
            body=body,
        )

    if status_name.lower() == "pending":
        do_transition(TRANSITION_PENDING_TO_TODO)
        issue = _http_json(method="GET", url=f"{base}/rest/api/3/issue/{issue_key}", headers=headers)
        issue_fields = issue.get("fields", {}) if isinstance(issue.get("fields"), dict) else {}
        status_name = str(((issue_fields.get("status") or {}) or {}).get("name") or "").strip()

    if status_name.lower() in {"to do", "todo"}:
        do_transition(TRANSITION_TODO_TO_IN_PROGRESS)

    # Post-In-Progress: fill required fields, add comment, then Ready For Review
    # Re-fetch status after transitions
    issue = _http_json(method="GET", url=f"{base}/rest/api/3/issue/{issue_key}", headers=headers)
    issue_fields = issue.get("fields", {}) if isinstance(issue.get("fields"), dict) else {}
    status_name = str(((issue_fields.get("status") or {}) or {}).get("name") or "").strip()

    if status_name.lower() == "in progress":
        # Discover remaining fields
        what_field_id = _find_field_id(fields, field_name=FIELD_WHAT_DID_I_WORK_ON)
        impacted_field_id = _find_field_id(fields, field_name=FIELD_WHAT_MIGHT_BE_IMPACTED)
        classification_field_id = _find_field_id(fields, field_name=FIELD_CLASSIFICATION)
        service_field_id = _find_field_id(fields, field_name=FIELD_SERVICE)
        bug_impact_field_id = _find_field_id(fields, field_name=FIELD_BUG_IMPACT)
        developer_rca_field_id = _find_first_field_id(
            fields,
            field_names=FIELD_DEVELOPER_RCA_CANDIDATE_NAMES,
        )

        missing_fields = [
            name
            for name, fid in [
                (FIELD_WHAT_DID_I_WORK_ON, what_field_id),
                (FIELD_WHAT_MIGHT_BE_IMPACTED, impacted_field_id),
                (FIELD_CLASSIFICATION, classification_field_id),
                (FIELD_SERVICE, service_field_id),
                (FIELD_BUG_IMPACT, bug_impact_field_id),
                ("/".join(FIELD_DEVELOPER_RCA_CANDIDATE_NAMES), developer_rca_field_id),
            ]
            if not fid
        ]
        if missing_fields:
            raise RuntimeError(f"Missing Jira field(s): {', '.join(missing_fields)}")

        # Fresh editmeta for allowedValues/schema
        editmeta = _http_json(
            method="GET",
            url=f"{base}/rest/api/3/issue/{issue_key}/editmeta",
            headers=headers,
        )
        edit_fields = (editmeta or {}).get("fields", {}) if isinstance(editmeta, dict) else {}

        classification_meta = edit_fields.get(classification_field_id, {})
        service_meta = edit_fields.get(service_field_id, {})
        bug_impact_meta = edit_fields.get(bug_impact_field_id, {})
        developer_rca_meta = edit_fields.get(developer_rca_field_id, {})

        classification_allowed = classification_meta.get("allowedValues") or []
        service_allowed = service_meta.get("allowedValues") or []
        bug_impact_allowed = bug_impact_meta.get("allowedValues") or []
        developer_rca_allowed = developer_rca_meta.get("allowedValues") or []

        if not isinstance(classification_allowed, list) or not classification_allowed:
            raise RuntimeError(f"No allowedValues for {FIELD_CLASSIFICATION!r}")
        if not isinstance(service_allowed, list) or not service_allowed:
            raise RuntimeError(f"No allowedValues for {FIELD_SERVICE!r}")
        if not isinstance(bug_impact_allowed, list) or not bug_impact_allowed:
            raise RuntimeError(f"No allowedValues for {FIELD_BUG_IMPACT!r}")
        if not isinstance(developer_rca_allowed, list) or not developer_rca_allowed:
            raise RuntimeError("No allowedValues for Developer RCA (required for Ready For Review)")

        class_opt = _pick_allowed_option(classification_allowed, CLASSIFICATION_VALUE)
        svc_opt = _pick_allowed_option(service_allowed, SERVICE_VALUE)
        bug_opt = _pick_allowed_option(bug_impact_allowed, BUG_IMPACT_VALUE)
        if not class_opt:
            raise RuntimeError(f"Could not find {FIELD_CLASSIFICATION} option {CLASSIFICATION_VALUE!r}")
        if not svc_opt:
            raise RuntimeError(f"Could not find {FIELD_SERVICE} option {SERVICE_VALUE!r}")
        if not bug_opt:
            raise RuntimeError(f"Could not find {FIELD_BUG_IMPACT} option {BUG_IMPACT_VALUE!r}")

        class_payload_raw = {"id": str(class_opt["id"])} if class_opt.get("id") else {"value": CLASSIFICATION_VALUE}
        svc_payload_raw = {"id": str(svc_opt["id"])} if svc_opt.get("id") else {"value": SERVICE_VALUE}
        bug_payload_raw = {"id": str(bug_opt["id"])} if bug_opt.get("id") else {"value": BUG_IMPACT_VALUE}

        fields_to_set: dict[str, Any] = {}

        # Only fill text fields if empty
        if _is_empty_value(issue_fields.get(what_field_id)) or _is_empty_value(issue_fields.get(impacted_field_id)):
            what_text = ""
            impacted_text = ""
            # Prefer runner output JSON (from ticket_runner) when available.
            try:
                if isinstance(runner_json, dict) and runner_json:
                    what_text = str(runner_json.get("WHAT_DID_I_WORK_ON_TECH_PM") or "").strip()
                    impacted_text = str(runner_json.get("WHAT_MIGHT_BE_IMPACTED") or "").strip()
            except Exception:
                what_text = ""
                impacted_text = ""

            if not what_text or not impacted_text:
                what_text, impacted_text = _cursor_generate_work_and_impact(
                    issue_key=issue_key,
                    summary=summary,
                    description_text=description_text,
                )
            if _is_empty_value(issue_fields.get(what_field_id)):
                fields_to_set[what_field_id] = what_text
            if _is_empty_value(issue_fields.get(impacted_field_id)):
                fields_to_set[impacted_field_id] = impacted_text

        # Always ensure select fields
        fields_to_set[classification_field_id] = _apply_option_payload_schema(
            classification_meta, class_payload_raw
        )
        fields_to_set[service_field_id] = _apply_option_payload_schema(service_meta, svc_payload_raw)
        fields_to_set[bug_impact_field_id] = _apply_option_payload_schema(bug_impact_meta, bug_payload_raw)

        # Ensure required transition field (only set if missing)
        # Prefer runner output RCA if provided; otherwise fall back to default value.
        desired_dev_rca = DEVELOPER_RCA_DEFAULT_VALUE
        try:
            if isinstance(runner_json, dict) and runner_json:
                desired_dev_rca = str(runner_json.get("RCA") or "").strip() or desired_dev_rca
        except Exception:
            desired_dev_rca = DEVELOPER_RCA_DEFAULT_VALUE

        rca_opt = _pick_allowed_option(developer_rca_allowed, desired_dev_rca) or _pick_allowed_option(
            developer_rca_allowed, DEVELOPER_RCA_DEFAULT_VALUE
        )
        if not rca_opt:
            sample = ", ".join(
                str(o.get("value") or o.get("name") or "") for o in developer_rca_allowed[:15]
            )
            raise RuntimeError(
                f"Could not find Developer RCA option {desired_dev_rca!r} (or fallback {DEVELOPER_RCA_DEFAULT_VALUE!r}). First allowed: {sample}"
            )
        rca_payload_raw = (
            {"id": str(rca_opt["id"])} if rca_opt.get("id") else {"value": str(rca_opt.get('value') or rca_opt.get('name') or desired_dev_rca)}
        )
        developer_rca_payload = _apply_option_payload_schema(developer_rca_meta, rca_payload_raw)
        if _is_empty_value(issue_fields.get(developer_rca_field_id)):
            fields_to_set[developer_rca_field_id] = developer_rca_payload

        if fields_to_set:
            if args.dry_run:
                print(
                    f"[dry-run] Would update In-Progress fields on {issue_key}: {json.dumps(fields_to_set, ensure_ascii=False)}"
                )
            # First attempt: send as-is.
            try:
                if not args.dry_run:
                    _http_json(
                        method="PUT",
                        url=f"{base}/rest/api/3/issue/{issue_key}",
                        headers=headers,
                        body={"fields": fields_to_set},
                    )
            except RuntimeError as e:
                # Fallback: if Jira complains a field must be ADF, retry those text fields as ADF.
                detail = str(e)
                if "Atlassian Document" in detail:
                    patched = dict(fields_to_set)
                    if what_field_id in patched and isinstance(patched[what_field_id], str):
                        patched[what_field_id] = _adf_doc(patched[what_field_id])
                    if impacted_field_id in patched and isinstance(patched[impacted_field_id], str):
                        patched[impacted_field_id] = _adf_doc(patched[impacted_field_id])
                    if args.dry_run:
                        print(
                            f"[dry-run] Would retry In-Progress fields as ADF on {issue_key}: {json.dumps(patched, ensure_ascii=False)}"
                        )
                    else:
                        _http_json(
                            method="PUT",
                            url=f"{base}/rest/api/3/issue/{issue_key}",
                            headers=headers,
                            body={"fields": patched},
                        )
                else:
                    raise

        # Add comment if not already present (check last 50)
        comments = _http_json(
            method="GET",
            url=f"{base}/rest/api/3/issue/{issue_key}/comment?maxResults=50",
            headers=headers,
        )
        existing = (comments or {}).get("comments", []) if isinstance(comments, dict) else []
        already = False
        if isinstance(existing, list):
            for c in existing:
                body = (c or {}).get("body")
                if BUGBOT_COMMENT_TEXT.strip().lower() == _adf_text(body).strip().lower():
                    already = True
                    break
        if not already:
            if args.dry_run:
                print(f"[dry-run] Would add comment on {issue_key}: {BUGBOT_COMMENT_TEXT!r}")
            else:
                _http_json(
                    method="POST",
                    url=f"{base}/rest/api/3/issue/{issue_key}/comment",
                    headers=headers,
                    body={"body": _adf_doc(BUGBOT_COMMENT_TEXT)},
                )

        # Transition to Ready for Review
        # Include Developer RCA in the transition payload as well, since some Jira workflows enforce
        # required fields at transition time (transition screen) and do not accept a separate prior PUT.
        do_transition(
            TRANSITION_IN_PROGRESS_TO_READY_FOR_REVIEW,
            transition_fields={developer_rca_field_id: developer_rca_payload},
        )

    print(
        f"Updated {issue_key}: refinement={REFINEMENT_TARGET_VALUE}, "
        f"estimate_set={'yes' if did_set_estimate else 'no'}, "
        f"estimate_default={DEFAULT_ORIGINAL_ESTIMATE}, "
        f"transitions=({TRANSITION_PENDING_TO_TODO}, {TRANSITION_TODO_TO_IN_PROGRESS}, {TRANSITION_IN_PROGRESS_TO_READY_FOR_REVIEW})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


