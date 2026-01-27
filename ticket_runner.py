#!/usr/bin/env python3
"""
Ticket runner:
- karamba new <NUMBER>
- fetch LAB-<NUMBER> details from Jira REST API
- invoke cursor-agent in a PLAN-style interactive session following local PLAN.md
- require cursor-agent to output COMMIT_NAME: ... and print it (no local fallback)

Then the script will:
- stage tracked changes + newly created files from this run (avoids accidentally adding pre-existing untracked files)
- git commit -m "<COMMIT_NAME from Cursor>"
- git push --force (sets upstream to origin/<current-branch> if needed)
- karamba pr LAB-<NUMBER>
"""

from __future__ import annotations

import argparse
import base64
import datetime
import errno
import json
import os
import re
import select
import subprocess
import sys
import textwrap
import termios
import threading
import time
import tty
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional


BUGBOT_FILES_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_JIRA_BASE_URL = "https://labguru.atlassian.net"
DEFAULT_PROJECT_PREFIX = "LAB"
DEFAULT_REPO_DIR = "/Users/jonathaneshel/Desktop/Code/Labguru"
DEFAULT_CURSOR_LOG_FILE = os.path.join(BUGBOT_FILES_DIR, "logs", "last_cursor_agent.log")
DEBUG_NDJSON_LOG_PATH = os.path.join(BUGBOT_FILES_DIR, ".cursor", "debug.log")
DEBUG_RUN_ID = f"run-{int(time.time())}"
BUGBOT_JIRA_LABEL = "BugBot"

PLAN_MD_PATH = os.path.join(BUGBOT_FILES_DIR, "PLAN.md")
API_RULES_MD_PATH = os.path.join(BUGBOT_FILES_DIR, "api summary.md")
BUGBOT_TEACHER_CURSORRULES_PATH = (
    "/Users/jonathaneshel/Desktop/Code/DS/app/services/protocol_converter/.cursorrules"
)
JIRA_INSTRUCTIONS_PATH = os.path.join(BUGBOT_FILES_DIR, "JIRA_INSTRUCTIONS")
_JIRA_FIELDS_JSON_BEGIN = "[JIRA_FIELDS_JSON]"
_JIRA_FIELDS_JSON_END = "[/JIRA_FIELDS_JSON]"

REVIEW_CONTEXT_DIR = os.path.join(BUGBOT_FILES_DIR, "review_context")
MAX_REVIEW_CONTEXT_DESCRIPTION_CHARS = 8000
MAX_REVIEW_CONTEXT_DIFF_CHARS = 12000


@dataclass(frozen=True)
class JiraIssue:
    key: str
    url: str
    summary: str
    issue_type: str
    priority: str
    description_text: str


@dataclass(frozen=True)
class RunnerOutput:
    commit_name: str
    what_did_i_work_on_dev: str
    what_did_i_work_on_tech_pm: str
    what_did_i_work_on_non_tech_pm: str
    what_might_be_impacted: str
    rca: str
    # Optional; not required in RUNNER_OUTPUT anymore.
    rca_comments: str
    specs_status: str
    specs_details: str


class PrCreationError(RuntimeError):
    def __init__(self, message: str, *, output: str) -> None:
        super().__init__(message)
        self.output = output


def _log(message: str) -> None:
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[ticket_runner {ts}] {message}", file=sys.stderr, flush=True)

# #region agent log (debug ndjson)
def _debug_log(hypothesis_id: str, location: str, message: str, data: dict[str, Any]) -> None:
    """
    NDJSON debug log (no secrets).
    Writes to /Users/jonathaneshel/Desktop/Code/bugbot files/.cursor/debug.log
    """
    try:
        payload = {
            "sessionId": "debug-session",
            "runId": DEBUG_RUN_ID,
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        os.makedirs(os.path.dirname(DEBUG_NDJSON_LOG_PATH), exist_ok=True)
        with open(DEBUG_NDJSON_LOG_PATH, "ab") as f:
            f.write(json.dumps(payload).encode("utf-8", errors="replace") + b"\n")
    except Exception:
        # Never crash the script due to debug logging
        pass

# #endregion agent log (debug ndjson)

def _write_log_line(log_fp: Optional[object], message: str) -> None:
    if log_fp is None:
        return
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        log_fp.write(f"[ticket_runner {ts}] {message}\n".encode("utf-8", errors="replace"))
    except Exception:
        pass


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def _read_text_if_exists(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


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

def _truncate(s: str, max_chars: int) -> str:
    s = (s or "").strip()
    if len(s) <= max_chars:
        return s
    return s[:max_chars].rstrip() + "\n\n(TRUNCATED)"


def _safe_filename_component(s: str) -> str:
    """
    Conservative filename sanitization (cross-platform-ish).
    Keeps letters/numbers/._- and replaces everything else with '-'.
    """
    s = (s or "").strip()
    s = s.replace(os.sep, "-")
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-") or "unknown"


def _allocate_review_context_path(*, ticket_number: str) -> str:
    """
    Returns a non-existing path under REVIEW_CONTEXT_DIR.
    Base filename: <ticket_number>.md; if exists, append __2, __3, ...
    """
    os.makedirs(REVIEW_CONTEXT_DIR, exist_ok=True)
    ticket_part = _safe_filename_component(ticket_number)
    base = os.path.join(REVIEW_CONTEXT_DIR, f"{ticket_part}.md")
    if not os.path.exists(base):
        return base
    i = 2
    while True:
        candidate = os.path.join(REVIEW_CONTEXT_DIR, f"{ticket_part}__{i}.md")
        if not os.path.exists(candidate):
            return candidate
        i += 1


def _parse_pr_url_from_karamba_output(text: str) -> Optional[str]:
    """
    Prefer the canonical karamba success line:
      Pull request created: https://...
    Fall back to first URL in output.
    """
    raw = text or ""
    for line in raw.splitlines():
        m = re.search(r"^\s*Pull request created:\s*(https?://\S+)\s*$", line.strip())
        if m:
            return m.group(1).strip().rstrip(").,]")
    m2 = re.search(r"https?://\S+", raw)
    if m2:
        return m2.group(0).strip().rstrip(").,]")
    return None


def _git_origin_head_branch(repo_dir: str) -> str:
    """
    Returns something like 'main' from 'origin/main' by reading origin/HEAD.
    Falls back to 'main' if not available.
    """
    res = _run_cmd(
        cmd=["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        cwd=repo_dir,
        capture_output=True,
        check=False,
    )
    val = (res.stdout or "").strip()
    if val.startswith("origin/"):
        candidate = val[len("origin/") :].strip()
        if candidate:
            return candidate
    return "main"


def _read_karamba_env(repo_dir: str) -> dict[str, str]:
    """
    Reads a simple KEY=VALUE file from <repo_dir>/.karamba.
    Values may be quoted. Unknown/invalid lines are ignored.
    """
    path = os.path.join(repo_dir, ".karamba")
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return {}
    except Exception:
        return {}

    env: dict[str, str] = {}
    for raw in lines:
        line = (raw or "").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = (key or "").strip()
        val = (val or "").strip()
        if not key:
            continue
        # Strip optional surrounding quotes
        if len(val) >= 2 and ((val[0] == val[-1] == '"') or (val[0] == val[-1] == "'")):
            val = val[1:-1]
        env[key] = val
    return env


def _default_pr_base_branch(repo_dir: str) -> tuple[str, str]:
    """
    Best-effort: choose the branch that PRs are typically opened against.
    Prefer .karamba STAGING_BRANCH (git_flow), else MAIN_BRANCH, else origin/HEAD-derived.
    Returns (branch_name, source_note).
    """
    cfg = _read_karamba_env(repo_dir)
    staging = (cfg.get("STAGING_BRANCH") or "").strip()
    if staging:
        return staging, "from .karamba STAGING_BRANCH"
    main = (cfg.get("MAIN_BRANCH") or "").strip()
    if main:
        return main, "from .karamba MAIN_BRANCH"
    return _git_origin_head_branch(repo_dir), "from origin/HEAD"


def _resolve_base_ref(repo_dir: str, base_branch: str) -> tuple[str, str]:
    """
    Resolve a base branch name into an existing ref we can diff against.
    Prefers origin/<branch>, then local <branch>. Falls back to origin/HEAD-derived.
    Returns (base_ref, note) where base_ref is like 'origin/staging' or 'staging'.
    """
    base_branch = (base_branch or "").strip()
    if not base_branch:
        base_branch, src = _default_pr_base_branch(repo_dir)
    else:
        src = "provided"

    origin_ref = f"refs/remotes/origin/{base_branch}"
    local_ref = f"refs/heads/{base_branch}"

    has_origin = _run_cmd(
        cmd=["git", "show-ref", "--verify", "--quiet", origin_ref],
        cwd=repo_dir,
        capture_output=True,
        check=False,
    ).returncode == 0
    if has_origin:
        return f"origin/{base_branch}", src

    has_local = _run_cmd(
        cmd=["git", "show-ref", "--verify", "--quiet", local_ref],
        cwd=repo_dir,
        capture_output=True,
        check=False,
    ).returncode == 0
    if has_local:
        return base_branch, src

    fallback, fallback_src = _default_pr_base_branch(repo_dir)
    # If that fallback is also missing, we still return origin/<fallback> (git diff will just be empty/error,
    # but the packet will include the chosen base for debugging).
    return f"origin/{fallback}", f"fallback ({fallback_src})"


def write_pr_review_context_file(
    *,
    ticket_number: str,
    repo_dir: str,
    issue: "JiraIssue",
    runner_output: "RunnerOutput",
    pr_url: Optional[str],
    pr_create_output: str,
    pr_create_failed: bool,
) -> str:
    os.makedirs(REVIEW_CONTEXT_DIR, exist_ok=True)

    branch = _git_current_branch(repo_dir)
    path = _allocate_review_context_path(ticket_number=ticket_number)

    head_sha_res = _run_cmd(
        cmd=["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
        check=False,
    )
    head_sha = (head_sha_res.stdout or "").strip() if head_sha_res.returncode == 0 else "(unknown)"

    base_branch, base_branch_source = _default_pr_base_branch(repo_dir)
    base_ref, base_ref_source = _resolve_base_ref(repo_dir, base_branch)
    diff_range = f"{base_ref}...HEAD"

    name_status = _run_cmd(
        cmd=["git", "diff", "--name-status", diff_range],
        cwd=repo_dir,
        capture_output=True,
        check=False,
    ).stdout or ""

    diff_stat = _run_cmd(
        cmd=["git", "diff", "--stat", diff_range],
        cwd=repo_dir,
        capture_output=True,
        check=False,
    ).stdout or ""

    patch = _run_cmd(
        cmd=["git", "diff", diff_range],
        cwd=repo_dir,
        capture_output=True,
        check=False,
    ).stdout or ""
    patch = _truncate(patch, MAX_REVIEW_CONTEXT_DIFF_CHARS)

    description = _truncate(issue.description_text or "(No Jira description provided)", MAX_REVIEW_CONTEXT_DESCRIPTION_CHARS)
    pr_create_output = (pr_create_output or "").strip()
    pr_status = "not created" if pr_create_failed else (pr_url or "(unknown)")

    generated_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    md = textwrap.dedent(
        f"""
        # PR Review Context — {issue.key}

        Generated: {generated_at}
        Repo: {repo_dir}
        Branch: {branch}
        HEAD: {head_sha}
        Base branch: {base_branch} ({base_branch_source})
        Base ref used: {base_ref} ({base_ref_source})
        Diff range: {diff_range}
        PR: {pr_status}

        ## Ticket
        - Key: {issue.key}
        - URL: {issue.url}
        - Summary: {issue.summary}
        - Type: {issue.issue_type}
        - Priority: {issue.priority}

        ### Jira description (truncated)
        {description}

        ## Implementation summary (from Cursor RUNNER_OUTPUT)
        - COMMIT_NAME: {runner_output.commit_name}
        - WHAT_DID_I_WORK_ON_DEV: {runner_output.what_did_i_work_on_dev}
        - WHAT_DID_I_WORK_ON_TECH_PM: {runner_output.what_did_i_work_on_tech_pm}
        - WHAT_DID_I_WORK_ON_NON_TECH_PM: {runner_output.what_did_i_work_on_non_tech_pm}
        - WHAT_MIGHT_BE_IMPACTED: {runner_output.what_might_be_impacted}
        - RCA: {runner_output.rca}
        - RCA_COMMENTS: {runner_output.rca_comments}

        ## Changed files
        ```
        {name_status.strip()}
        ```

        ## Diff stat
        ```
        {diff_stat.strip()}
        ```

        ## Patch (truncated)
        ```diff
        {patch.strip()}
        ```

        ## PR creation output
        PR creation failed: {str(bool(pr_create_failed))}
        ```
        {pr_create_output}
        ```

        ## When replying to review comments
        - Paste the unresolved review thread(s) here (comment text + file:line).
        - Then ask Cursor: \"Address these comments with the minimal change; update tests if needed.\"
        """
    ).strip() + "\n"

    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    return path


def _extract_ticket_number(raw: str) -> str:
    """
    Accepts:
    - "1234"
    - "LAB-1234"
    - "lab-1234"
    - "https://.../browse/LAB-1234"
    Returns: "1234"
    """
    m = re.search(r"\b(\d+)\b", raw)
    if not m:
        raise ValueError(f"Could not find a ticket number in: {raw!r}")
    return m.group(1)


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


def _jira_basic_auth_header(email: str, api_token: str) -> str:
    raw = f"{email}:{api_token}".encode("utf-8")
    b64 = base64.b64encode(raw).decode("ascii")
    return f"Basic {b64}"


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


def _http_get_json(url: str, headers: dict[str, str]) -> Any:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


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
        raise RuntimeError(
            f"Jira API request failed ({e.code}) for {api_url}. Details: {detail}"
        ) from e
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


def run_karamba_new(ticket_number: str) -> None:
    run_karamba_new_in_repo(
        issue_key=f"{DEFAULT_PROJECT_PREFIX}-{ticket_number}",
        repo_dir=DEFAULT_REPO_DIR,
    )


def run_karamba_new_in_repo(*, issue_key: str, repo_dir: str) -> None:
    subprocess.run(["karamba", "new", issue_key], check=True, cwd=repo_dir)


def build_cursor_prompt(issue: JiraIssue) -> str:
    plan_md = _read_text(PLAN_MD_PATH)
    api_rules = _read_text(API_RULES_MD_PATH)
    bugbot_rules = _read_text(BUGBOT_TEACHER_CURSORRULES_PATH)
    jira_instructions_raw = _read_text_if_exists(JIRA_INSTRUCTIONS_PATH)
    jira_prompt_instructions, _jira_cfg = _parse_jira_instructions(jira_instructions_raw)

    description = issue.description_text
    if not description:
        description = "(No Jira description provided)"

    # Keep prompt bounded (avoid gigantic ADF dumps)
    description = description.strip()
    if len(description) > 8000:
        description = description[:8000].rstrip() + "\n\n(TRUNCATED)"

    return textwrap.dedent(
        f"""
        You are working on Jira ticket {issue.key}.
        URL: {issue.url}
        Summary: {issue.summary}
        Type: {issue.issue_type}
        Priority: {issue.priority}

        {"Jira field-filling instructions (follow these when writing RUNNER_OUTPUT fields):" if jira_prompt_instructions else ""}
        {"--- BEGIN JIRA_INSTRUCTIONS ---" if jira_prompt_instructions else ""}
        {jira_prompt_instructions if jira_prompt_instructions else ""}
        {"--- END JIRA_INSTRUCTIONS ---" if jira_prompt_instructions else ""}

        You MUST fix this ticket according to @bugbot files/PLAN.md.
        (For convenience, the full contents of that file are included below as PLAN.md.)

        Jira description (plain text):
        {description}

        Follow this process exactly (PLAN.md):
        --- BEGIN PLAN.md ---
        {plan_md}
        --- END PLAN.md ---

        Extra API/spec rules (only apply if relevant to this ticket):
        --- BEGIN api summary.md ---
        {api_rules}
        --- END api summary.md ---

        Bugbot Teacher Cursor rules (treat these as the active cursorrules):
        --- BEGIN .cursorrules (bugbot teacher) ---
        {bugbot_rules}
        --- END .cursorrules ---

        Requirements:
        - Fix the ticket according to @bugbot files/PLAN.md (included above).
        - If you need clarification, ask up to 2 questions (only if truly needed) in language a non-technical PM would understand.
          Wait for answers in the terminal.
          If you are still blocked after 2 questions (or answers are not provided), proceed with best-guess assumptions.
        - Proceed with the plan and execution following PLAN.md.
        - At the end, output EXACTLY the following block (nothing extra before/after it).
          It must be machine-parseable and each field must be on ONE line:

          RUNNER_OUTPUT_BEGIN
          COMMIT_NAME: <your proposed commit message>
          WHAT_DID_I_WORK_ON_DEV: <3 short bullet-style items, in a single line, separated by "; ">
          WHAT_DID_I_WORK_ON_TECH_PM: <3 short bullet-style items, in a single line, separated by "; ">
          WHAT_DID_I_WORK_ON_NON_TECH_PM: <3 short bullet-style items, in a single line, separated by "; ">
          WHAT_MIGHT_BE_IMPACTED: <1 or 2 big-picture impacts, in a single line, separated by "; ">
          RCA: <must be EXACTLY one of the allowed values below>
          RCA_COMMENTS: <required line; if none, leave empty after the colon>
          SPECS_STATUS: <must be EXACTLY one of: OK; FAILED; NOT_RUN>
          SPECS_DETAILS: <required line; include the spec command(s) you ran; if FAILED include brief failure; if NOT_RUN include why>
          RUNNER_OUTPUT_END

        - Allowed RCA values (pick exactly one, copy/paste exactly):
          - Requirement gaps
          - Incorrect logic implementation
          - Missing edge case handling
          - Incomplete or invalid input validation
          - Async / timing / race condition
          - API misuse or faulty integration
          - Unhandled null / undefined values
          - Code merge conflict or overwrite
          - State management issue
          - Refactoring side effect
          - Lack of unit test coverage
          - Other

        - Do NOT commit or push anything.
        """
    ).strip() + "\n"


def run_cursor_agent_interactive(
    *,
    prompt: str,
    repo_dir: str,
    cursor_log_file: Optional[str],
    heartbeat_seconds: int,
    cursor_bin: str = "cursor-agent",
) -> None:
    """
    Runs cursor-agent attached to the terminal for interactive Q&A.
    """
    # Interactive mode:
    # - user can answer questions in-terminal
    # - we capture output so we can extract the final structured runner output block
    output, returncode = _run_cursor_agent_with_pty_capture(
        prompt=prompt,
        repo_dir=repo_dir,
        cursor_log_file=cursor_log_file,
        heartbeat_seconds=heartbeat_seconds,
        cursor_bin=cursor_bin,
    )
    if returncode != 0:
        raise RuntimeError("cursor-agent failed (see output above).")

    try:
        runner_output = extract_runner_output_from_text(output)
    except RuntimeError as e:
        msg = str(e)
        if "Missing required runner output fields" in msg:
            _log("Runner output missing required fields; re-asking Cursor once to emit the full block.")
            followup_prompt = textwrap.dedent(
                """
                Your previous response was missing required RUNNER_OUTPUT fields.
                Output ONLY the full machine-parseable block, including ALL required fields, and nothing else:

                RUNNER_OUTPUT_BEGIN
                COMMIT_NAME: <your proposed commit message>
                WHAT_DID_I_WORK_ON_DEV: <...>
                WHAT_DID_I_WORK_ON_TECH_PM: <...>
                WHAT_DID_I_WORK_ON_NON_TECH_PM: <...>
                WHAT_MIGHT_BE_IMPACTED: <...>
                RCA: <...>
                SPECS_STATUS: <must be EXACTLY one of: OK; FAILED; NOT_RUN>
                SPECS_DETAILS: <required; include the spec command(s) you ran; if FAILED include brief failure; if NOT_RUN include why>
                RUNNER_OUTPUT_END
                """
            ).strip() + "\n"
            output = run_cursor_agent_capture_output(
                prompt=followup_prompt,
                repo_dir=repo_dir,
                cursor_log_file=cursor_log_file,
                timeout_seconds=600,
                cursor_bin=cursor_bin,
            )
            runner_output = extract_runner_output_from_text(output)
        else:
            raise
    print("\nRUNNER OUTPUT (from Cursor):")
    print(f"COMMIT_NAME: {runner_output.commit_name}")
    print(f"WHAT_DID_I_WORK_ON_DEV: {runner_output.what_did_i_work_on_dev}")
    print(f"WHAT_DID_I_WORK_ON_TECH_PM: {runner_output.what_did_i_work_on_tech_pm}")
    print(f"WHAT_DID_I_WORK_ON_NON_TECH_PM: {runner_output.what_did_i_work_on_non_tech_pm}")
    print(f"WHAT_MIGHT_BE_IMPACTED: {runner_output.what_might_be_impacted}")
    print(f"RCA: {runner_output.rca}")
    print(f"RCA_COMMENTS: {runner_output.rca_comments}")
    print(f"SPECS_STATUS: {runner_output.specs_status}")
    print(f"SPECS_DETAILS: {runner_output.specs_details}")


def _prepare_cursor_invocation(*, prompt: str, cursor_bin: str) -> tuple[list[str], str]:
    """
    Returns (cmd, effective_prompt) for a single attempt.
    Always uses `/plan` embedded in the prompt (this Cursor CLI does not support `--mode`).
    """
    return [cursor_bin], "/plan\n\n" + prompt


def _run_cursor_agent_with_pty_capture(
    *,
    prompt: str,
    repo_dir: str,
    cursor_log_file: Optional[str],
    heartbeat_seconds: int,
    cursor_bin: str,
) -> tuple[str, int]:
    """
    Runs cursor-agent connected to a PTY, forwarding stdin/stdout and capturing output.
    Returns (captured_output, returncode).
    """
    import pty

    cmd, effective_prompt = _prepare_cursor_invocation(prompt=prompt, cursor_bin=cursor_bin)

    pid, master_fd = pty.fork()
    if pid == 0:
        os.chdir(repo_dir)
        os.execvp(cmd[0], cmd)

    # #region agent log (debug ndjson)
    _debug_log(
        "H1",
        "ticket_runner.py:_run_cursor_agent_with_pty_capture",
        "pty_fork_parent",
        {
            "repo_dir": repo_dir,
            "cursor_bin": cursor_bin,
            "child_pid": pid,
            "master_fd": master_fd,
            "stdin_isatty": bool(sys.stdin.isatty()),
        },
    )
    # #endregion agent log (debug ndjson)

    captured: list[bytes] = []
    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()

    log_fp = None
    if cursor_log_file:
        try:
            log_path = os.path.abspath(os.path.expanduser(cursor_log_file))
            parent = os.path.dirname(log_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            # Overwrite each run so the default log stays small and always reflects the latest run.
            log_fp = open(log_path, "wb", buffering=0)
            _log(f"Streaming cursor-agent output to log file: {log_path}")
            _write_log_line(log_fp, f"repo_dir={repo_dir}")
            _write_log_line(log_fp, f"cursor_bin={cursor_bin}")
        except Exception as e:
            _log(f"Could not open cursor log file ({cursor_log_file}): {e}")
            log_fp = None

    # Stream the initial prompt in chunks (it can be very large and can block if written in one call).
    pending_to_master = bytearray(effective_prompt.encode("utf-8"))
    total_initial_bytes = len(pending_to_master)
    if total_initial_bytes > 0:
        _log(f"Sending initial prompt to cursor-agent ({total_initial_bytes} bytes)...")
        _write_log_line(log_fp, f"Sending initial prompt to cursor-agent ({total_initial_bytes} bytes)")
        # #region agent log (debug ndjson)
        _debug_log(
            "H2",
            "ticket_runner.py:_run_cursor_agent_with_pty_capture",
            "initial_prompt_prepared",
            {"total_initial_bytes": total_initial_bytes},
        )
        # #endregion agent log (debug ndjson)

    old_tty_attrs = None
    did_log_raw_mode = False
    did_log_first_write = False
    did_log_first_read = False
    did_log_submit = False
    try:
        try:
            old_tty_attrs = termios.tcgetattr(stdin_fd)
            # cbreak keeps signals (Ctrl+C) working; raw mode disables ISIG.
            tty.setcbreak(stdin_fd)
            if not did_log_raw_mode:
                # #region agent log (debug ndjson)
                _debug_log(
                    "H3",
                    "ticket_runner.py:_run_cursor_agent_with_pty_capture",
                    "stdin_setcbreak_ok",
                    {"stdin_fd": stdin_fd},
                )
                # #endregion agent log (debug ndjson)
                did_log_raw_mode = True
        except Exception:
            old_tty_attrs = None
            if not did_log_raw_mode:
                # #region agent log (debug ndjson)
                _debug_log(
                    "H3",
                    "ticket_runner.py:_run_cursor_agent_with_pty_capture",
                    "stdin_setcbreak_failed",
                    {"stdin_fd": stdin_fd},
                )
                # #endregion agent log (debug ndjson)
                did_log_raw_mode = True

        stop_event = threading.Event()
        runner_output_detected: dict[str, bool] = {}

        def read_master() -> None:
            nonlocal did_log_first_read
            while not stop_event.is_set():
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                captured.append(data)
                try:
                    os.write(stdout_fd, data)
                except Exception:
                    pass
                if log_fp is not None:
                    try:
                        log_fp.write(data)
                    except Exception:
                        pass
                if not did_log_first_read:
                    # #region agent log (debug ndjson)
                    _debug_log(
                        "H6",
                        "ticket_runner.py:_run_cursor_agent_with_pty_capture",
                        "first_read_from_cursor",
                        {"read_bytes": len(data)},
                    )
                    # #endregion agent log (debug ndjson)
                    did_log_first_read = True

                # Look for the end marker in the streaming output.
                # Cursor output may include ANSI; keep it simple and search for the literal token.
                try:
                    text = data.decode("utf-8", errors="ignore")
                except Exception:
                    text = ""
                if "RUNNER_OUTPUT_END" in text and not runner_output_detected.get("done"):
                    # #region agent log (debug ndjson)
                    _debug_log(
                        "H8",
                        "ticket_runner.py:_run_cursor_agent_with_pty_capture",
                        "runner_output_end_detected_stream",
                        {},
                    )
                    # #endregion agent log (debug ndjson)
                    runner_output_detected["done"] = True
                    stop_event.set()
                    break

        def write_master() -> None:
            nonlocal did_log_first_write, did_log_submit
            while not stop_event.is_set():
                if pending_to_master:
                    try:
                        chunk = bytes(pending_to_master[:4096])
                        written = os.write(master_fd, chunk)
                        if not did_log_first_write:
                            # #region agent log (debug ndjson)
                            _debug_log(
                                "H5",
                                "ticket_runner.py:_run_cursor_agent_with_pty_capture",
                                "first_write_attempt",
                                {
                                    "attempt_bytes": len(chunk),
                                    "written": int(written),
                                    "remaining": len(pending_to_master) - int(written),
                                },
                            )
                            # #endregion agent log (debug ndjson)
                            did_log_first_write = True
                        if written > 0:
                            del pending_to_master[:written]
                            if not pending_to_master and total_initial_bytes > 0:
                                _log("Initial prompt sent.")
                                _write_log_line(log_fp, "Initial prompt sent.")
                                # Submit the pasted prompt (Cursor TUI treats large pastes as draft input).
                                try:
                                    os.write(master_fd, b"\r")
                                    if not did_log_submit:
                                        _write_log_line(log_fp, "Sent submit key: CR")
                                        # #region agent log (debug ndjson)
                                        _debug_log(
                                            "H7",
                                            "ticket_runner.py:_run_cursor_agent_with_pty_capture",
                                            "submit_key_sent",
                                            {"key": "CR"},
                                        )
                                        # #endregion agent log (debug ndjson)
                                        did_log_submit = True
                                except Exception:
                                    pass
                    except OSError as e:
                        if e.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
                            continue
                        break
                    except Exception:
                        break
                    continue

                # Interactive mode: forward stdin to cursor-agent so it can ask clarifying questions.
                try:
                    r, _, _ = select.select([stdin_fd], [], [], 0.1)
                except Exception:
                    stop_event.wait(0.1)
                    continue
                if not r:
                    continue
                try:
                    user_bytes = os.read(stdin_fd, 1024)
                except Exception:
                    continue
                if not user_bytes:
                    continue
                try:
                    os.write(master_fd, user_bytes)
                except Exception:
                    break

        # #region agent log (debug ndjson)
        _debug_log(
            "H4",
            "ticket_runner.py:_run_cursor_agent_with_pty_capture",
            "threads_starting",
            {"pending_bytes": len(pending_to_master)},
        )
        # #endregion agent log (debug ndjson)

        t_read = threading.Thread(target=read_master, daemon=True)
        t_write = threading.Thread(target=write_master, daemon=True)
        t_read.start()
        t_write.start()

        # #region agent log (debug ndjson)
        _debug_log(
            "H4",
            "ticket_runner.py:_run_cursor_agent_with_pty_capture",
            "threads_started",
            {},
        )
        # #endregion agent log (debug ndjson)

        # Wait until we either detect RUNNER_OUTPUT_END, or the process exits.
        while not stop_event.is_set():
            try:
                finished_pid, _ = os.waitpid(pid, os.WNOHANG)
            except Exception:
                finished_pid = 0
            if finished_pid == pid:
                stop_event.set()
                break
            stop_event.wait(0.2)

        # If we detected the final output marker, stop cursor-agent so the script can finish automatically.
        if runner_output_detected.get("done"):
            try:
                os.kill(pid, 15)
                # #region agent log (debug ndjson)
                _debug_log(
                    "H9",
                    "ticket_runner.py:_run_cursor_agent_with_pty_capture",
                    "terminated_cursor_after_runner_output",
                    {},
                )
                # #endregion agent log (debug ndjson)
            except Exception:
                pass

        try:
            os.waitpid(pid, 0)
        except Exception:
            pass
        try:
            t_read.join(timeout=1.0)
            t_write.join(timeout=1.0)
        except Exception:
            pass
        return b"".join(captured).decode("utf-8", errors="replace"), 0
    finally:
        if log_fp is not None:
            try:
                log_fp.close()
            except Exception:
                pass
        if old_tty_attrs is not None:
            try:
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_tty_attrs)
            except Exception:
                pass
    return b"".join(captured).decode("utf-8", errors="replace"), 1


def extract_commit_name_from_text(output: str) -> str:
    """
    Strict: require a COMMIT_NAME line.
    We do NOT guess or fallback.
    """
    for line in output.splitlines():
        if line.startswith("COMMIT_NAME:"):
            value = line[len("COMMIT_NAME:") :].strip()
            if not value:
                raise RuntimeError("Cursor returned COMMIT_NAME but it was empty.")
            return value
    raise RuntimeError("Did not find required `COMMIT_NAME:` line in Cursor output.")


def extract_runner_output_from_text(output: str) -> RunnerOutput:
    """
    Strict: require the RUNNER_OUTPUT_BEGIN/END block and all required fields.
    """
    allowed_rca = {
        "Requirement gaps",
        "Incorrect logic implementation",
        "Missing edge case handling",
        "Incomplete or invalid input validation",
        "Async / timing / race condition",
        "API misuse or faulty integration",
        "Unhandled null / undefined values",
        "Code merge conflict or overwrite",
        "State management issue",
        "Refactoring side effect",
        "Lack of unit test coverage",
        "Other",
        "Other (add in comments)",
    }

    in_block = False
    fields: dict[str, str] = {}

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line == "RUNNER_OUTPUT_BEGIN":
            in_block = True
            continue
        if line == "RUNNER_OUTPUT_END":
            in_block = False
            break
        if not in_block:
            continue

        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        key = k.strip()
        value = v.strip()
        fields[key] = value

    required_keys = {
        "COMMIT_NAME",
        "WHAT_DID_I_WORK_ON_DEV",
        "WHAT_DID_I_WORK_ON_TECH_PM",
        "WHAT_DID_I_WORK_ON_NON_TECH_PM",
        "WHAT_MIGHT_BE_IMPACTED",
        "RCA",
        "RCA_COMMENTS",
        "SPECS_STATUS",
        "SPECS_DETAILS",
    }
    missing = sorted(required_keys - set(fields.keys()))
    if missing:
        raise RuntimeError(f"Missing required runner output fields: {', '.join(missing)}")

    commit_name = fields["COMMIT_NAME"]
    if not commit_name:
        raise RuntimeError("Runner output COMMIT_NAME was empty.")

    rca = fields["RCA"]
    if rca == "Other (add in comments)":
        rca = "Other"
    if rca not in allowed_rca:
        raise RuntimeError(
            "Runner output RCA was not one of the allowed values. "
            f"Got: {rca!r}"
        )

    specs_status = fields["SPECS_STATUS"]
    if specs_status not in {"OK", "FAILED", "NOT_RUN"}:
        raise RuntimeError(
            "Runner output SPECS_STATUS must be EXACTLY one of: OK; FAILED; NOT_RUN. "
            f"Got: {specs_status!r}"
        )

    return RunnerOutput(
        commit_name=commit_name,
        what_did_i_work_on_dev=fields["WHAT_DID_I_WORK_ON_DEV"],
        what_did_i_work_on_tech_pm=fields["WHAT_DID_I_WORK_ON_TECH_PM"],
        what_did_i_work_on_non_tech_pm=fields["WHAT_DID_I_WORK_ON_NON_TECH_PM"],
        what_might_be_impacted=fields["WHAT_MIGHT_BE_IMPACTED"],
        rca=rca,
        rca_comments=fields["RCA_COMMENTS"],
        specs_status=specs_status,
        specs_details=fields["SPECS_DETAILS"],
    )


def _run_cmd(
    *,
    cmd: list[str],
    cwd: str,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        check=check,
        capture_output=capture_output,
    )


def _git_current_branch(repo_dir: str) -> str:
    res = _run_cmd(
        cmd=["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
    )
    return (res.stdout or "").strip()


def _sanitize_commit_message(raw: str) -> str:
    # Git commit subject should be a single line; keep it robust against accidental newlines/ANSI.
    msg = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw or "")  # strip common ANSI escapes
    msg = msg.replace("\r", "\n").split("\n", 1)[0].strip()
    if not msg:
        raise RuntimeError("Commit message resolved to empty after sanitization.")
    return msg


def _ensure_git_repo(repo_dir: str) -> None:
    res = _run_cmd(
        cmd=["git", "rev-parse", "--is-inside-work-tree"],
        cwd=repo_dir,
        capture_output=True,
        check=False,
    )
    if res.returncode != 0 or "true" not in (res.stdout or ""):
        raise RuntimeError(f"Not a git repository: {repo_dir}")


def _git_untracked_files(repo_dir: str) -> set[str]:
    """
    Returns a set of untracked file paths relative to repo root.
    Uses NUL-separated porcelain for safe parsing.
    """
    res = _run_cmd(
        cmd=["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=repo_dir,
        capture_output=True,
        check=False,
    )
    out = res.stdout or ""
    untracked: set[str] = set()
    for entry in out.split("\0"):
        if not entry:
            continue
        # Untracked entries look like: "?? path"
        if entry.startswith("?? "):
            path = entry[3:].strip()
            if path:
                untracked.add(path)
    return untracked


def _chunked(items: list[str], size: int) -> list[list[str]]:
    if size <= 0:
        return [items]
    return [items[i : i + size] for i in range(0, len(items), size)]


def run_git_commit_push_and_pr(
    *,
    repo_dir: str,
    issue_key: str,
    commit_name_from_cursor: str,
    preexisting_untracked: Optional[set[str]] = None,
    create_pr: bool = True,
) -> tuple[Optional[str], str]:
    _ensure_git_repo(repo_dir)

    commit_message = _sanitize_commit_message(commit_name_from_cursor)
    branch = _git_current_branch(repo_dir)
    if not branch:
        raise RuntimeError("Could not determine current git branch.")

    if issue_key not in commit_message:
        _log(
            f"WARNING: commit message does not include {issue_key}. "
            "Proceeding anyway (Cursor is the source of truth)."
        )

    if preexisting_untracked is None:
        _log("Staging changes (git add -A)...")
        _run_cmd(cmd=["git", "add", "-A"], cwd=repo_dir)
    else:
        _log("Staging changes (tracked changes + newly created files from this run)...")
        # Tracked modifications/deletions:
        _run_cmd(cmd=["git", "add", "-u"], cwd=repo_dir)

        # Only stage untracked files that appeared during this run (avoid scooping up old junk).
        post_untracked = _git_untracked_files(repo_dir)
        new_untracked = sorted(post_untracked - preexisting_untracked)
        if new_untracked:
            _log(f"Staging {len(new_untracked)} newly created file(s) from this run...")
            for chunk in _chunked(new_untracked, 100):
                _run_cmd(cmd=["git", "add", "--", *chunk], cwd=repo_dir)

    _log(f"Committing changes (git commit -m {commit_message!r})...")
    commit_res = _run_cmd(
        cmd=["git", "commit", "-m", commit_message],
        cwd=repo_dir,
        check=False,
        capture_output=True,
    )
    if commit_res.returncode != 0:
        stderr = (commit_res.stderr or "").strip()
        stdout = (commit_res.stdout or "").strip()
        # Common "nothing to commit" case: allow continuing to push/pr.
        if "nothing to commit" in (stderr + "\n" + stdout).lower():
            _log("No changes to commit (working tree clean). Continuing to push/PR.")
        else:
            raise RuntimeError(
                "git commit failed.\n"
                f"stdout:\n{stdout}\n\n"
                f"stderr:\n{stderr}\n"
            )

    _log(f"Force pushing branch to origin (git push --force --set-upstream origin {branch})...")
    _run_cmd(cmd=["git", "push", "--force", "--set-upstream", "origin", branch], cwd=repo_dir)

    if not create_pr:
        _log("Skipping PR creation (redo-pr mode).")
        return None, ""

    _log(f"Creating PR via karamba (karamba pr {issue_key})...")
    pr_res = _run_cmd(
        cmd=["karamba", "pr", issue_key],
        cwd=repo_dir,
        check=False,
        capture_output=True,
    )
    pr_out = ((pr_res.stdout or "") + "\n" + (pr_res.stderr or "")).strip()
    if pr_res.returncode != 0:
        raise PrCreationError("karamba pr failed.", output=pr_out)

    pr_url = _parse_pr_url_from_karamba_output(pr_out)
    if pr_url:
        _log(f"Detected PR URL: {pr_url}")
    else:
        _log("Could not detect PR URL from karamba output (continuing).")
    return pr_url, pr_out


def run_cursor_agent_capture_output(
    *,
    prompt: str,
    repo_dir: str,
    cursor_log_file: Optional[str],
    timeout_seconds: int,
    retries: int = 0,
    cursor_bin: str = "cursor-agent",
) -> str:
    """
    Non-interactive capture. This is primarily used to get the COMMIT_NAME line.
    If Cursor needs interactive clarifications, prefer the interactive runner.
    """
    cmd, effective_prompt = _prepare_cursor_invocation(prompt=prompt, cursor_bin=cursor_bin)
    # #region agent log (debug ndjson)
    _debug_log(
        "H10",
        "ticket_runner.py:run_cursor_agent_capture_output",
        "cursor_headless_start",
        {"repo_dir": repo_dir, "cursor_bin": cursor_bin, "timeout_seconds": timeout_seconds},
    )
    # #endregion agent log (debug ndjson)
    last_err: Optional[str] = None
    last_stdout: str = ""
    last_stderr: str = ""
    attempt_count = max(1, int(retries) + 1)

    for attempt_idx in range(attempt_count):
        res = subprocess.run(
            cmd,
            input=effective_prompt,
            text=True,
            capture_output=True,
            cwd=repo_dir,
            timeout=timeout_seconds,
        )
        last_stdout = res.stdout or ""
        last_stderr = res.stderr or ""

        # #region agent log (debug ndjson)
        _debug_log(
            "H11",
            "ticket_runner.py:run_cursor_agent_capture_output",
            "cursor_headless_end",
            {
                "returncode": int(res.returncode),
                "stdout_len": len(last_stdout),
                "stderr_len": len(last_stderr),
                "attempt": attempt_idx + 1,
                "attempts_total": attempt_count,
            },
        )
        # #endregion agent log (debug ndjson)

        if res.returncode == 0:
            last_err = None
            break

        err_msg = (last_stderr or "").strip() or "cursor-agent failed"
        last_err = err_msg
        if "connection stalled" in err_msg.lower() and attempt_idx < attempt_count - 1:
            _log(f"cursor-agent failed with 'Connection stalled' (attempt {attempt_idx+1}/{attempt_count}); retrying...")
            continue
        break
    if cursor_log_file:
        try:
            log_path = os.path.abspath(os.path.expanduser(cursor_log_file))
            parent = os.path.dirname(log_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            # Overwrite each run so the default log stays small and always reflects the latest run.
            with open(log_path, "wb") as f:
                f.write(last_stdout.encode("utf-8", errors="replace"))
                if last_stderr.strip():
                    f.write(b"\n\n--- STDERR ---\n")
                    f.write(last_stderr.encode("utf-8", errors="replace"))
            _log(f"Wrote cursor-agent output to log file: {log_path}")
        except Exception as e:
            _log(f"Could not write cursor log file ({cursor_log_file}): {e}")
    if last_err is not None:
        raise RuntimeError(last_err)
    return last_stdout


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Start a Jira ticket, fetch details, and run cursor-agent plan flow."
    )
    parser.add_argument("ticket", help="Ticket number (e.g. 1234) or LAB-1234 or Jira URL")
    parser.add_argument(
        "--jira-base-url",
        default=os.environ.get("JIRA_BASE_URL", DEFAULT_JIRA_BASE_URL),
        help=f"Jira base URL (default: {DEFAULT_JIRA_BASE_URL} or env JIRA_BASE_URL)",
    )
    parser.add_argument(
        "--project-prefix",
        default=DEFAULT_PROJECT_PREFIX,
        help=f"Jira project prefix (default: {DEFAULT_PROJECT_PREFIX})",
    )
    parser.add_argument(
        "--cursor-bin",
        default="cursor-agent",
        help="Binary name/path for cursor-agent (default: cursor-agent).",
    )
    parser.add_argument(
        "--repo-dir",
        default=DEFAULT_REPO_DIR,
        help=f"Repo directory where karamba + cursor-agent should run (default: {DEFAULT_REPO_DIR}).",
    )
    parser.add_argument(
        "--cursor-log-file",
        default=DEFAULT_CURSOR_LOG_FILE,
        help=f"File path to stream/capture cursor-agent output (default: {DEFAULT_CURSOR_LOG_FILE}).",
    )
    parser.add_argument(
        "--no-cursor-log-file",
        action="store_true",
        help="Disable writing any cursor-agent log file.",
    )
    parser.add_argument(
        "--heartbeat-seconds",
        type=int,
        default=20,
        help="While cursor-agent is quiet, print a status heartbeat every N seconds (0 disables).",
    )
    parser.add_argument(
        "--cursor-timeout-seconds",
        type=int,
        default=600,
        help="Timeout for cursor-agent in headless mode (seconds).",
    )
    parser.add_argument(
        "--cursor-retries",
        type=int,
        default=2,
        help="Retries for transient cursor-agent failures (e.g., 'Connection stalled').",
    )
    parser.add_argument(
        "--no-jira-field-update",
        action="store_true",
        help="Disable updating Jira fields from JIRA_INSTRUCTIONS (even if configured).",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Run cursor-agent headlessly (no stdin forwarding for clarifying questions).",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Run cursor-agent interactively (stdin forwarded). Overrides --non-interactive.",
    )
    parser.add_argument(
        "--redo-pr",
        action="store_true",
        help="Redo an existing PR: do NOT run karamba new and do NOT run karamba pr. "
        "Assumes you already checked out the PR branch locally; will run Cursor, commit, and force-push.",
    )

    args = parser.parse_args(argv)
    ticket_number = _extract_ticket_number(args.ticket)
    repo_dir = os.path.abspath(os.path.expanduser(args.repo_dir))
    if not os.path.isdir(repo_dir):
        print(f"Repo directory does not exist: {repo_dir}", file=sys.stderr)
        return 2
    cursor_log_file = None if args.no_cursor_log_file else args.cursor_log_file

    jira_email = os.environ.get("JIRA_EMAIL", "").strip()
    jira_token = os.environ.get("JIRA_API_TOKEN", "").strip()
    if not jira_email or not jira_token:
        print("Missing Jira credentials.", file=sys.stderr)
        print("Set env vars: JIRA_EMAIL and JIRA_API_TOKEN", file=sys.stderr)
        return 2

    issue_key = f"{args.project_prefix}-{ticket_number}"
    if not args.redo_pr:
        print(f"Starting work: karamba new {issue_key}")
        run_karamba_new_in_repo(issue_key=issue_key, repo_dir=repo_dir)
    else:
        _log("redo-pr mode enabled: skipping karamba new; operating on current git branch.")

    # Snapshot untracked files BEFORE cursor-agent runs, so we don't accidentally add pre-existing
    # untracked artifacts from the developer machine into the PR.
    preexisting_untracked = _git_untracked_files(repo_dir)

    print(f"Fetching Jira issue LAB-{ticket_number} ...")
    issue = fetch_jira_issue(
        jira_base_url=args.jira_base_url,
        project_prefix=args.project_prefix,
        ticket_number=ticket_number,
        email=jira_email,
        api_token=jira_token,
    )

    print(f"Ticket: {issue.key} — {issue.summary}")
    print(f"URL: {issue.url}")

    # Ensure the ticket is labeled so it’s easy to find BugBot-handled tickets.
    # fetch_jira_issue returns a JiraIssue dataclass; we need labels from the raw issue JSON.
    # We re-fetch once (cheap) to avoid changing fetch_jira_issue’s return type.
    try:
        raw_issue = _http_get_json(
            f"{args.jira_base_url.rstrip('/')}/rest/api/3/issue/{issue_key}",
            headers={
                "Authorization": _jira_basic_auth_header(jira_email, jira_token),
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
                jira_base_url=args.jira_base_url,
                issue_key=issue_key,
                email=jira_email,
                api_token=jira_token,
                fields_payload={"labels": new_labels},
            )
            _log("Jira label added.")
    except Exception as e:
        _log(f"WARNING: failed to ensure Jira label {BUGBOT_JIRA_LABEL!r}: {e}")

    prompt = build_cursor_prompt(issue)

    # Default: headless. Opt-in to interactive.
    if args.interactive and sys.stdin.isatty():
        _log(f"Running cursor-agent in PTY interactive mode in {repo_dir} (stdin forwarded).")
        output, returncode = _run_cursor_agent_with_pty_capture(
            prompt=prompt,
            repo_dir=repo_dir,
            cursor_log_file=cursor_log_file,
            heartbeat_seconds=args.heartbeat_seconds,
            cursor_bin=args.cursor_bin,
        )
        if returncode != 0:
            raise RuntimeError("cursor-agent failed (see output above).")
    else:
        _log(f"Running cursor-agent headlessly in {repo_dir} (no human input).")
        output = run_cursor_agent_capture_output(
            prompt=prompt,
            repo_dir=repo_dir,
            cursor_log_file=cursor_log_file,
            timeout_seconds=args.cursor_timeout_seconds,
            retries=args.cursor_retries,
            cursor_bin=args.cursor_bin,
        )
    allowed_rca_lines = textwrap.dedent(
        """
        - Requirement gaps
        - Incorrect logic implementation
        - Missing edge case handling
        - Incomplete or invalid input validation
        - Async / timing / race condition
        - API misuse or faulty integration
        - Unhandled null / undefined values
        - Code merge conflict or overwrite
        - State management issue
        - Refactoring side effect
        - Lack of unit test coverage
        - Other
        """
    ).strip()

    def reask_cursor_for_full_block(*, reason: str) -> str:
        _log(f"{reason} Re-asking Cursor once to emit the full block.")
        followup_prompt = textwrap.dedent(
            f"""
            Your previous response was invalid.

            Requirements:
            - Output ONLY the full machine-parseable block below (nothing else).
            - RCA MUST be EXACTLY one of the allowed values listed.
            - If you need to say something else, set RCA: Other and put details in RCA_COMMENTS.

            Allowed RCA values (pick exactly one):
            {allowed_rca_lines}

            RUNNER_OUTPUT_BEGIN
            COMMIT_NAME: <your proposed commit message>
            WHAT_DID_I_WORK_ON_DEV: <...>
            WHAT_DID_I_WORK_ON_TECH_PM: <...>
            WHAT_DID_I_WORK_ON_NON_TECH_PM: <...>
            WHAT_MIGHT_BE_IMPACTED: <...>
            RCA: <one of the allowed values>
            RCA_COMMENTS: <required; may be empty after colon>
            SPECS_STATUS: <must be EXACTLY one of: OK; FAILED; NOT_RUN>
            SPECS_DETAILS: <required; include the spec command(s) you ran; if FAILED include brief failure; if NOT_RUN include why>
            RUNNER_OUTPUT_END
            """
        ).strip() + "\n"
        return run_cursor_agent_capture_output(
            prompt=followup_prompt,
            repo_dir=repo_dir,
            cursor_log_file=cursor_log_file,
            timeout_seconds=args.cursor_timeout_seconds,
            retries=args.cursor_retries,
            cursor_bin=args.cursor_bin,
        )

    try:
        runner_output = extract_runner_output_from_text(output)
    except RuntimeError as e:
        msg = str(e)
        if "Missing required runner output fields" in msg:
            output2 = reask_cursor_for_full_block(reason="Runner output missing required fields.")
            runner_output = extract_runner_output_from_text(output2)
        elif "Runner output RCA was not one of the allowed values" in msg:
            output2 = reask_cursor_for_full_block(reason="Runner output RCA invalid.")
            runner_output = extract_runner_output_from_text(output2)
        else:
            raise

    print("\nRUNNER OUTPUT (from Cursor):")
    print(f"COMMIT_NAME: {runner_output.commit_name}")
    print(f"WHAT_DID_I_WORK_ON_DEV: {runner_output.what_did_i_work_on_dev}")
    print(f"WHAT_DID_I_WORK_ON_TECH_PM: {runner_output.what_did_i_work_on_tech_pm}")
    print(f"WHAT_DID_I_WORK_ON_NON_TECH_PM: {runner_output.what_did_i_work_on_non_tech_pm}")
    print(f"WHAT_MIGHT_BE_IMPACTED: {runner_output.what_might_be_impacted}")
    print(f"RCA: {runner_output.rca}")
    print(f"SPECS_STATUS: {runner_output.specs_status}")
    print(f"SPECS_DETAILS: {runner_output.specs_details}")

    if not args.no_jira_field_update:
        jira_instructions_raw = _read_text_if_exists(JIRA_INSTRUCTIONS_PATH)
        _jira_prompt_instructions, jira_cfg = _parse_jira_instructions(jira_instructions_raw)
        if jira_cfg.get("enabled") and jira_cfg.get("updates"):
            updates = jira_cfg["updates"]
            if not isinstance(updates, list):
                raise RuntimeError("JIRA_INSTRUCTIONS config: 'updates' must be a list")

            fields_payload: dict[str, Any] = {}
            for u in updates:
                if not isinstance(u, dict):
                    raise RuntimeError("JIRA_INSTRUCTIONS config: each update must be an object")
                field_id = (u.get("jira_field_id") or "").strip()
                source = (u.get("source") or "").strip()
                fmt = (u.get("format") or "as_is").strip()
                append_source = (u.get("append_source") or "").strip()

                if not field_id or not source:
                    raise RuntimeError("JIRA_INSTRUCTIONS config: update requires jira_field_id and source")

                value = _runner_output_value(runner_output, source)
                append_value = _runner_output_value(runner_output, append_source) if append_source else ""
                fields_payload[field_id] = _format_for_jira(value, fmt, append_value=append_value)

            _log(f"Updating Jira fields for {issue_key} based on JIRA_INSTRUCTIONS ...")
            update_jira_issue_fields(
                jira_base_url=args.jira_base_url,
                issue_key=issue_key,
                email=jira_email,
                api_token=jira_token,
                fields_payload=fields_payload,
            )
            _log("Jira fields updated.")

    pr_url: Optional[str] = None
    pr_create_output: str = ""
    pr_create_failed = False
    try:
        pr_url, pr_create_output = run_git_commit_push_and_pr(
            repo_dir=repo_dir,
            issue_key=issue_key,
            commit_name_from_cursor=runner_output.commit_name,
            preexisting_untracked=preexisting_untracked,
            create_pr=not args.redo_pr,
        )
    except PrCreationError as e:
        pr_create_failed = True
        pr_create_output = e.output
        _log("PR creation failed. Writing review context packet anyway, then re-raising.")

    wrote_path = write_pr_review_context_file(
        ticket_number=ticket_number,
        repo_dir=repo_dir,
        issue=issue,
        runner_output=runner_output,
        pr_url=pr_url,
        pr_create_output=pr_create_output,
        pr_create_failed=pr_create_failed,
    )
    print(f"\nWrote PR review context file:\n{wrote_path}")

    if pr_create_failed:
        raise PrCreationError("karamba pr failed (see PR creation output in context file).", output=pr_create_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


