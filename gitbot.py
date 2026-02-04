#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

from runner.jira import fetch_jira_issue


BUGBOT_FILES_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_REVIEW_CONTEXT_DIR = os.path.join(BUGBOT_FILES_DIR, "review_context")
DEFAULT_REPO_DIR = "/Users/jonathaneshel/Desktop/Code/Labguru"
DEFAULT_DB_PATH = os.path.join(BUGBOT_FILES_DIR, ".gitbot_state.sqlite3")

DEFAULT_GITHUB_OWNER = "BioData"
DEFAULT_GITHUB_REPO = "Labguru"

GITHUB_API_BASE = "https://api.github.com"

DEFAULT_JIRA_BASE_URL = "https://labguru.atlassian.net"
JIRA_BASE_URL = os.getenv("JIRA_BASE_URL", DEFAULT_JIRA_BASE_URL).rstrip("/")
JIRA_EMAIL = (os.getenv("JIRA_EMAIL", "") or "").strip()
JIRA_API_TOKEN = (os.getenv("JIRA_API_TOKEN", "") or "").strip()

# When auto-generating review_context, keep the diff bounded.
MAX_AUTOGEN_PR_DIFF_CHARS = int(os.getenv("GITBOT_AUTOGEN_PR_DIFF_MAX_CHARS", "120000"))


def _now_ts() -> int:
    return int(time.time())


def _utc_iso(ts: float | int) -> str:
    return datetime.datetime.utcfromtimestamp(float(ts)).replace(tzinfo=datetime.timezone.utc).isoformat()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _json_b64url(obj: dict[str, Any]) -> str:
    raw = json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _b64url(raw)


def _log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[gitbot {ts}] {msg}", file=sys.stderr, flush=True)


def _http_request(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    data: Optional[bytes] = None,
    timeout: float = 30.0,
) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url=url, method=method.upper(), data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", 200))
            resp_headers = {k.lower(): v for (k, v) in resp.headers.items()}
            body = resp.read() or b""
            return status, resp_headers, body
    except urllib.error.HTTPError as e:
        body = e.read() if hasattr(e, "read") else b""
        headers2 = {k.lower(): v for (k, v) in (e.headers.items() if e.headers else [])}
        return int(getattr(e, "code", 0) or 0), headers2, body


def _http_json(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    payload: Optional[dict[str, Any]] = None,
    timeout: float = 30.0,
) -> tuple[int, dict[str, str], Any, str]:
    body: Optional[bytes] = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers = {**headers, "Content-Type": "application/json"}
    status, resp_headers, raw = _http_request(method=method, url=url, headers=headers, data=body, timeout=timeout)
    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        return status, resp_headers, None, text
    try:
        return status, resp_headers, json.loads(text), text
    except Exception:
        return status, resp_headers, None, text


class StateDB:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def _ensure_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS kv (
              k TEXT PRIMARY KEY,
              v TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS processed (
              platform TEXT NOT NULL,
              comment_kind TEXT NOT NULL,
              comment_id INTEGER NOT NULL,
              repo_owner TEXT NOT NULL,
              repo_name TEXT NOT NULL,
              created_at TEXT,
              processed_at TEXT NOT NULL,
              ticket_key TEXT,
              reply_url TEXT,
              PRIMARY KEY (platform, comment_kind, comment_id)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_processed_repo ON processed(repo_owner, repo_name, processed_at)"
        )
        self._conn.commit()

    def get_kv(self, key: str) -> Optional[str]:
        row = self._conn.execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
        return str(row["v"]) if row else None

    def set_kv(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (key, value),
            )

    def get_cursor_iso(self, key: str) -> Optional[str]:
        v = self.get_kv(key)
        return v.strip() if v else None

    def set_cursor_iso(self, key: str, iso_ts: str) -> None:
        self.set_kv(key, iso_ts)

    def is_processed(self, *, platform: str, comment_kind: str, comment_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM processed WHERE platform = ? AND comment_kind = ? AND comment_id = ?",
            (platform, comment_kind, int(comment_id)),
        ).fetchone()
        return bool(row)

    def mark_processed(
        self,
        *,
        platform: str,
        comment_kind: str,
        comment_id: int,
        repo_owner: str,
        repo_name: str,
        created_at: str,
        ticket_key: str,
        reply_url: str,
    ) -> None:
        processed_at = _utc_iso(_now_ts())
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO processed(
                  platform, comment_kind, comment_id,
                  repo_owner, repo_name,
                  created_at, processed_at,
                  ticket_key, reply_url
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, comment_kind, comment_id) DO NOTHING
                """,
                (
                    platform,
                    comment_kind,
                    int(comment_id),
                    repo_owner,
                    repo_name,
                    created_at or "",
                    processed_at,
                    ticket_key or "",
                    reply_url or "",
                ),
            )


def _parse_link_header(link_value: str) -> dict[str, str]:
    """
    Parses GitHub Link header:
      <https://...page=2>; rel="next", <...>; rel="last"
    Returns: {"next": "https://...", "last": "...", ...}
    """
    out: dict[str, str] = {}
    raw = (link_value or "").strip()
    if not raw:
        return out
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    for p in parts:
        m = re.match(r'^\s*<([^>]+)>\s*;\s*rel="([^"]+)"\s*$', p)
        if not m:
            continue
        out[m.group(2)] = m.group(1)
    return out


def _gh_api_json_paginated(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    timeout: float = 30.0,
    max_pages: int = 20,
) -> list[Any]:
    items: list[Any] = []
    next_url: Optional[str] = url
    pages = 0
    while next_url and pages < max_pages:
        pages += 1
        status, resp_headers, data, text = _http_json(method=method, url=next_url, headers=headers, timeout=timeout)
        if status < 200 or status >= 300:
            raise RuntimeError(f"GitHub API error (HTTP {status}) for {next_url}: {text[:2000]}")
        if isinstance(data, list):
            items.extend(data)
        elif data is None:
            break
        else:
            raise RuntimeError(f"Unexpected GitHub API response shape for {next_url}: {type(data).__name__}")
        link = _parse_link_header(resp_headers.get("link", ""))
        next_url = link.get("next")
    return items


@dataclass(frozen=True)
class CommentEvent:
    kind: str  # "review" | "issue"
    comment_id: int
    body: str
    created_at: str
    user_login: str
    html_url: str
    # For review comments
    pull_request_url: str
    # For issue comments
    issue_url: str
    # Optional review metadata
    file_path: str = ""
    line: int = 0
    diff_hunk: str = ""


@dataclass(frozen=True)
class PRInfo:
    number: int
    html_url: str
    title: str
    head_ref: str
    base_ref: str
    body: str


@dataclass(frozen=True)
class ResolvedContext:
    ticket_key: str
    context_path: str
    context_md: str
    pr: Optional[PRInfo]


def _write_review_context_markdown_autogen(
    *,
    review_context_dir: str,
    ticket_key: str,
    jira_issue,
    owner: str,
    repo: str,
    pr: Optional[PRInfo],
    pr_files: list[dict[str, Any]],
    pr_diff: str,
) -> str:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    path = _allocate_review_context_path(review_context_dir=review_context_dir, ticket_key=ticket_key)

    pr_url = pr.html_url if pr else ""
    pr_title = pr.title if pr else ""
    base_ref = pr.base_ref if pr else ""
    head_ref = pr.head_ref if pr else ""

    changed_list = "\n".join(
        f"• {str(f.get('filename') or '').strip()}"
        for f in pr_files
        if isinstance(f, dict) and str(f.get("filename") or "").strip()
    )
    if not changed_list:
        changed_list = "(No files list available)"

    diff_text = _truncate(pr_diff, MAX_AUTOGEN_PR_DIFF_CHARS)
    if not diff_text:
        diff_text = "(No diff available)"

    md = (
        f"# PR Review Context — {ticket_key}\n\n"
        f"Generated: {now}\n"
        f"Repo: {owner}/{repo}\n"
        f"PR: {pr_url or '(unknown)'}\n"
        f"Base: {base_ref or '(unknown)'}\n"
        f"Head: {head_ref or '(unknown)'}\n\n"
        f"## Ticket\n"
        f"• Key: {ticket_key}\n"
        f"• URL: {jira_issue.url}\n"
        f"• Summary: {jira_issue.summary}\n"
        f"• Type: {jira_issue.issue_type}\n"
        f"• Priority: {jira_issue.priority}\n\n"
        f"### Jira description\n"
        f"{jira_issue.description_text or '(No Jira description provided)'}\n\n"
        f"## PR\n"
        f"• Title: {pr_title}\n"
        f"• URL: {pr_url}\n\n"
        f"## Changed files\n"
        f"{changed_list}\n\n"
        f"## Patch (truncated)\n```diff\n{diff_text}\n```\n"
    )

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".review_context_", suffix=".md", dir=parent or None)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(md.rstrip() + "\n")
        os.replace(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
    return path


def _iso_to_dt(iso_ts: str) -> datetime.datetime:
    s = (iso_ts or "").strip()
    if not s:
        return datetime.datetime.fromtimestamp(0, tz=datetime.timezone.utc)
    # GitHub usually uses: 2026-01-21T00:49:43Z
    if s.endswith("Z"):
        dt = datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
        return dt
    try:
        # Fallback: fromisoformat supports +00:00 style
        dt2 = datetime.datetime.fromisoformat(s)
        return dt2 if dt2.tzinfo else dt2.replace(tzinfo=datetime.timezone.utc)
    except Exception:
        return datetime.datetime.fromtimestamp(0, tz=datetime.timezone.utc)


def _dt_to_cursor_iso(dt: datetime.datetime) -> str:
    dt = dt.astimezone(datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _mention_present(body: str, mention_token: str) -> bool:
    if not body or not mention_token:
        return False
    return mention_token.strip().lower() in body.lower()


def _extract_ticket_key(text: str, project_prefix: str) -> Optional[str]:
    s = (text or "").strip()
    if not s:
        return None
    pref = (project_prefix or "LAB").strip().upper()
    m = re.search(rf"\b{re.escape(pref)}-\d+\b", s, re.IGNORECASE)
    if not m:
        return None
    num = m.group(0).split("-", 1)[1]
    return f"{pref}-{num}"


def _strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", s or "")


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return _strip_ansi(f.read()).strip()


def _compact_context_md(full_md: str) -> str:
    s = (full_md or "").strip()
    m = re.search(r"^\s*##\s+Patch\b", s, flags=re.IGNORECASE | re.MULTILINE)
    if m:
        s = s[: m.start()].rstrip()
    max_chars = 12000
    if len(s) > max_chars:
        s = s[:max_chars].rstrip() + "\n\n(TRUNCATED)"
    return s


def _safe_filename_component(s: str) -> str:
    s = (s or "").strip()
    s = s.replace(os.sep, "-")
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-") or "unknown"


def _allocate_review_context_path(*, review_context_dir: str, ticket_key: str) -> str:
    os.makedirs(review_context_dir, exist_ok=True)
    ticket_part = _safe_filename_component(ticket_key)
    base = os.path.join(review_context_dir, f"{ticket_part}.md")
    if not os.path.exists(base):
        return base
    i = 2
    while True:
        candidate = os.path.join(review_context_dir, f"{ticket_part}__{i}.md")
        if not os.path.exists(candidate):
            return candidate
        i += 1


def _truncate(s: str, max_chars: int) -> str:
    s2 = (s or "").replace("\r\n", "\n").strip()
    if max_chars <= 0:
        return ""
    if len(s2) <= max_chars:
        return s2
    return s2[:max_chars].rstrip() + "\n\n(TRUNCATED)"


def _list_review_context_files(review_context_dir: str) -> list[str]:
    try:
        names = os.listdir(review_context_dir)
    except FileNotFoundError:
        return []
    paths: list[str] = []
    for n in names:
        if not n.lower().endswith(".md"):
            continue
        p = os.path.join(review_context_dir, n)
        if os.path.isfile(p):
            paths.append(p)
    paths.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return paths


def _find_context_by_ticket_key(*, review_context_dir: str, ticket_key: str) -> Optional[str]:
    if not ticket_key:
        return None
    direct = os.path.join(review_context_dir, f"{ticket_key}.md")
    if os.path.exists(direct):
        return direct
    ticket_num = ticket_key.split("-", 1)[1]
    for p in _list_review_context_files(review_context_dir):
        low = os.path.basename(p).lower()
        if ticket_key.lower() in low or ticket_num in low:
            return p
    return None


def _find_context_by_pr_url(*, review_context_dir: str, pr_html_url: str) -> Optional[str]:
    if not pr_html_url:
        return None
    needle = pr_html_url.strip()
    for p in _list_review_context_files(review_context_dir):
        try:
            txt = _read_text(p)
        except Exception:
            continue
        if needle in txt:
            return p
    return None


def _parse_pr_number_from_url(pull_url: str) -> Optional[int]:
    s = (pull_url or "").strip()
    m = re.search(r"/pulls/(\d+)\b", s)
    if m:
        return int(m.group(1))
    m2 = re.search(r"/pull/(\d+)\b", s)
    if m2:
        return int(m2.group(1))
    return None


def _gh_get_json(*, auth: GitHubAppAuth, url: str) -> Any:
    token = auth.get_installation_token()
    status, _headers, data, text = _http_json(method="GET", url=url, headers=auth._inst_headers(token))
    if status < 200 or status >= 300:
        raise RuntimeError(f"GitHub API GET failed (HTTP {status}) for {url}: {text[:2000]}")
    return data


def _gh_get_text(*, auth: GitHubAppAuth, url: str, accept: str, timeout: float = 90.0) -> str:
    token = auth.get_installation_token()
    headers = {**auth._inst_headers(token), "Accept": accept}
    status, _resp_headers, body = _http_request(method="GET", url=url, headers=headers, timeout=timeout)
    text = (body or b"").decode("utf-8", errors="replace")
    if status < 200 or status >= 300:
        raise RuntimeError(f"GitHub API GET failed (HTTP {status}) for {url}: {text[:2000]}")
    return text


def _fetch_pr_files(*, auth: GitHubAppAuth, owner: str, repo: str, pr_number: int) -> list[dict[str, Any]]:
    token = auth.get_installation_token()
    url = f"{auth.api_base}/repos/{owner}/{repo}/pulls/{int(pr_number)}/files?per_page=100"
    data = _gh_api_json_paginated(method="GET", url=url, headers=auth._inst_headers(token))
    out: list[dict[str, Any]] = []
    for it in data:
        if isinstance(it, dict):
            out.append(it)
    return out


def _fetch_pr_diff(*, auth: GitHubAppAuth, owner: str, repo: str, pr_number: int) -> str:
    url = f"{auth.api_base}/repos/{owner}/{repo}/pulls/{int(pr_number)}"
    return _gh_get_text(auth=auth, url=url, accept="application/vnd.github.v3.diff", timeout=120.0)


def _fetch_pr_info(*, auth: GitHubAppAuth, owner: str, repo: str, pr_number: int) -> PRInfo:
    url = f"{auth.api_base}/repos/{owner}/{repo}/pulls/{int(pr_number)}"
    data = _gh_get_json(auth=auth, url=url)
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected PR payload for #{pr_number}: {type(data).__name__}")
    head = data.get("head") if isinstance(data.get("head"), dict) else {}
    base = data.get("base") if isinstance(data.get("base"), dict) else {}
    return PRInfo(
        number=int(data.get("number") or pr_number),
        html_url=str(data.get("html_url") or ""),
        title=str(data.get("title") or ""),
        head_ref=str((head or {}).get("ref") or ""),
        base_ref=str((base or {}).get("ref") or ""),
        body=str(data.get("body") or ""),
    )


def _issue_comment_pr_api_url(*, auth: GitHubAppAuth, issue_url: str) -> Optional[str]:
    if not issue_url:
        return None
    data = _gh_get_json(auth=auth, url=issue_url)
    if not isinstance(data, dict):
        return None
    pr = data.get("pull_request")
    if not isinstance(pr, dict):
        return None
    pr_url = str(pr.get("url") or "")
    return pr_url or None


def resolve_context_for_event(
    *,
    auth: GitHubAppAuth,
    owner: str,
    repo: str,
    review_context_dir: str,
    project_prefix: str,
    ev: CommentEvent,
) -> tuple[Optional[ResolvedContext], str]:
    ticket_key = _extract_ticket_key(ev.body, project_prefix) or ""
    pr: Optional[PRInfo] = None

    pr_api_url = ev.pull_request_url
    if ev.kind == "issue":
        pr_api_url = _issue_comment_pr_api_url(auth=auth, issue_url=ev.issue_url) or ""
        if not pr_api_url:
            return None, "This issue comment is not on a PR."

    pr_number = _parse_pr_number_from_url(pr_api_url)
    if pr_number:
        pr = _fetch_pr_info(auth=auth, owner=owner, repo=repo, pr_number=pr_number)
        if not ticket_key:
            ticket_key = (
                _extract_ticket_key(pr.title, project_prefix)
                or _extract_ticket_key(pr.head_ref, project_prefix)
                or _extract_ticket_key(pr.body, project_prefix)
                or ""
            )

    context_path: Optional[str] = None
    if ticket_key:
        context_path = _find_context_by_ticket_key(review_context_dir=review_context_dir, ticket_key=ticket_key)
    if not context_path and pr and pr.html_url:
        context_path = _find_context_by_pr_url(review_context_dir=review_context_dir, pr_html_url=pr.html_url)
        if context_path and not ticket_key:
            ticket_key = _extract_ticket_key(os.path.basename(context_path), project_prefix) or ticket_key

    if not ticket_key:
        pref = (project_prefix or "LAB").strip().upper()
        return None, f"Could not infer ticket key. Please include `{pref}-1234` in your @gitbot comment."
    if not context_path or not os.path.exists(context_path):
        # Auto-generate context if missing (Jira creds required).
        if not JIRA_EMAIL or not JIRA_API_TOKEN:
            return None, (
                f"Missing Jira credentials; cannot auto-generate `review_context` for `{ticket_key}`.\n"
                "Set env vars: `JIRA_EMAIL` and `JIRA_API_TOKEN` (and optionally `JIRA_BASE_URL`), then retry."
            )
        if not pr_number:
            return None, f"Could not determine PR number needed to generate context for `{ticket_key}`."

        ticket_number = ticket_key.split("-", 1)[1] if "-" in ticket_key else ""
        try:
            jira_issue = fetch_jira_issue(
                jira_base_url=JIRA_BASE_URL,
                project_prefix=(project_prefix or "LAB").strip().upper(),
                ticket_number=ticket_number,
                email=JIRA_EMAIL,
                api_token=JIRA_API_TOKEN,
            )
            pr_files = _fetch_pr_files(auth=auth, owner=owner, repo=repo, pr_number=int(pr_number))
            pr_diff = _fetch_pr_diff(auth=auth, owner=owner, repo=repo, pr_number=int(pr_number))
            wrote_path = _write_review_context_markdown_autogen(
                review_context_dir=review_context_dir,
                ticket_key=ticket_key,
                jira_issue=jira_issue,
                owner=owner,
                repo=repo,
                pr=pr,
                pr_files=pr_files,
                pr_diff=pr_diff,
            )
            context_path = wrote_path
        except Exception as e:
            return None, f"Failed to auto-generate `review_context` for `{ticket_key}`. Error: {e}"

    compact = _compact_context_md(_read_text(context_path))
    return ResolvedContext(ticket_key=ticket_key, context_path=context_path, context_md=compact, pr=pr), ""


def _strip_bot_mention_and_ticket(*, body: str, mention_token: str, ticket_key: str) -> str:
    s = (body or "").strip()
    if not s:
        return ""
    if mention_token:
        s = re.sub(re.escape(mention_token), "", s, flags=re.IGNORECASE).strip()
    if ticket_key:
        s = re.sub(rf"\b{re.escape(ticket_key)}\b", "", s, flags=re.IGNORECASE).strip()
        num = ticket_key.split("-", 1)[1]
        s = re.sub(rf"\b{re.escape(num)}\b", "", s).strip()
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _safe_join_repo_path(repo_dir: str, rel_path: str) -> Optional[str]:
    rel = (rel_path or "").lstrip("/").strip()
    if not rel:
        return None
    abs_repo = os.path.abspath(repo_dir)
    candidate = os.path.abspath(os.path.join(abs_repo, rel))
    if not candidate.startswith(abs_repo + os.sep):
        return None
    return candidate


def _read_code_excerpt(*, repo_dir: str, file_path: str, line: int) -> str:
    abs_path = _safe_join_repo_path(repo_dir, file_path)
    if not abs_path or not os.path.exists(abs_path):
        return ""
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except Exception:
        return ""

    if not lines:
        return ""

    if line and 1 <= int(line) <= len(lines):
        lo = max(1, int(line) - 6)
        hi = min(len(lines), int(line) + 6)
        chunk = "\n".join(f"{i:>5}: {lines[i-1]}" for i in range(lo, hi + 1))
        return f"File: {file_path}\n(around line {line})\n{chunk}"

    # Fallback: top of file
    hi2 = min(len(lines), 120)
    chunk2 = "\n".join(f"{i:>5}: {lines[i-1]}" for i in range(1, hi2 + 1))
    return f"File: {file_path}\n(top of file)\n{chunk2}"


def _build_answer_prompt(
    *,
    ticket_key: str,
    question: str,
    comment_url: str,
    context_md: str,
    pr: Optional[PRInfo],
    review_file_path: str,
    review_line: int,
    diff_hunk: str,
    code_excerpt: str,
) -> str:
    pr_part = ""
    if pr and (pr.title or pr.html_url):
        pr_part = f"PR: {pr.html_url or '(unknown)'}\nPR title: {pr.title}\nPR head: {pr.head_ref}\n"

    review_loc = ""
    if review_file_path:
        review_loc = f"Review location: {review_file_path}:{review_line or '?'}\n"

    dh = (diff_hunk or "").strip()
    if len(dh) > 1500:
        dh = dh[:1500].rstrip() + "\n(TRUNCATED)"

    excerpt = (code_excerpt or "").strip()
    if len(excerpt) > 4000:
        excerpt = excerpt[:4000].rstrip() + "\n(TRUNCATED)"

    return textwrap.dedent(
        f"""
        You are gitbot, a helpful engineering assistant responding to PR review questions.
        Important constraints:
        - You MUST NOT apply changes, commit, push, or create PRs. This reply is recommendation-only.
        - You MAY recommend making code changes, but do NOT output a patch/diff or detailed step-by-step instructions.
        - Use ONLY the provided context and code excerpts.
        - If key info is missing, say exactly what is missing and where to look.
        - Be concise and direct.
        - If you recommend code changes, you MUST reference a concrete location (file + function/class or file + line range) and a clear reason.

        GIT MODE:
        - Be skeptical: default to NO.
        - Say YES only if you can point to a concrete change (file/function + what to change) and a clear reason (bug/requirement gap).
        - If you lack enough evidence/context to propose a specific change, you MUST answer NO (reason: insufficient evidence).
        - If you answer NO due to insufficient evidence, add one short follow-up line asking for concrete evidence (repro/log/spec/policy).
        - At the VERY end of your answer, append exactly one extra line:
          GIT MODE: code change recommended: YES|NO — <short reason>
        - Keep it as the last line (no bullets).

        Ticket: {ticket_key}
        {pr_part}{review_loc}Comment URL: {comment_url}
        Question: {question}

        --- BEGIN PR REVIEW CONTEXT (compacted) ---
        {context_md}
        --- END PR REVIEW CONTEXT ---

        --- BEGIN REVIEW DIFF HUNK (if any) ---
        {dh}
        --- END REVIEW DIFF HUNK ---

        --- BEGIN LOCAL CODE EXCERPT (if any) ---
        {excerpt}
        --- END LOCAL CODE EXCERPT ---
        """
    ).strip()


GIT_MODE_RECOMMENDATION_PREFIX = "GIT MODE: code change recommended:"


def _ensure_single_trailing_git_mode_line(answer: str) -> str:
    """
    Ensures there is exactly one `GIT MODE: code change recommended: ...` line,
    and that it is the final line.
    Used only for LLM-generated answers (not for missing-context/error replies).
    """
    s = (answer or "").strip()
    if not s:
        return f"{GIT_MODE_RECOMMENDATION_PREFIX} NO — empty response"

    lines = s.splitlines()
    idxs = [i for i, line in enumerate(lines) if (line or "").strip().startswith(GIT_MODE_RECOMMENDATION_PREFIX)]
    if not idxs:
        return s + "\n" + f"{GIT_MODE_RECOMMENDATION_PREFIX} NO — missing recommendation line from model"

    # Keep the last occurrence and move it to the end; drop any earlier duplicates.
    keep_idx = idxs[-1]
    keep_line = (lines[keep_idx] or "").strip()
    kept_body = [line for i, line in enumerate(lines) if i not in idxs]
    kept_body = [ln.rstrip() for ln in kept_body if (ln or "").strip()]
    return ("\n".join(kept_body).rstrip() + "\n" + keep_line).strip()


def _run_cursor_agent(*, prompt: str, cursor_bin: str, model: str, timeout_seconds: float) -> str:
    cmd = [cursor_bin, "-p", prompt, "--model", model]
    try:
        p = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout_seconds)
    except FileNotFoundError:
        return f"Error: `{cursor_bin}` not found on PATH."
    except subprocess.TimeoutExpired:
        return f"Error: `{cursor_bin}` timed out after {timeout_seconds:.0f}s."

    out = (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")
    out = _strip_ansi(out).strip()
    if not out and p.returncode != 0:
        return f"Error: `{cursor_bin}` exited with code {p.returncode}."
    return out or "(no response)"


def _parse_issue_number_from_url(issue_url: str) -> Optional[int]:
    s = (issue_url or "").strip()
    m = re.search(r"/issues/(\d+)\b", s)
    if m:
        return int(m.group(1))
    return None


def _post_review_reply(
    *,
    auth: GitHubAppAuth,
    owner: str,
    repo: str,
    pr_number: int,
    in_reply_to_comment_id: int,
    body: str,
) -> str:
    token = auth.get_installation_token()
    url = f"{auth.api_base}/repos/{owner}/{repo}/pulls/{int(pr_number)}/comments"
    status, _headers, data, text = _http_json(
        method="POST",
        url=url,
        headers=auth._inst_headers(token),
        payload={"body": body, "in_reply_to": int(in_reply_to_comment_id)},
    )
    if status < 200 or status >= 300 or not isinstance(data, dict):
        raise RuntimeError(f"Failed to post review reply (HTTP {status}): {text[:2000]}")
    return str(data.get("html_url") or "")


def _post_issue_comment(
    *,
    auth: GitHubAppAuth,
    owner: str,
    repo: str,
    issue_number: int,
    body: str,
) -> str:
    token = auth.get_installation_token()
    url = f"{auth.api_base}/repos/{owner}/{repo}/issues/{int(issue_number)}/comments"
    status, _headers, data, text = _http_json(
        method="POST",
        url=url,
        headers=auth._inst_headers(token),
        payload={"body": body},
    )
    if status < 200 or status >= 300 or not isinstance(data, dict):
        raise RuntimeError(f"Failed to post issue comment (HTTP {status}): {text[:2000]}")
    return str(data.get("html_url") or "")


def _format_reply_markdown(*, asker_login: str, question: str, answer: str) -> str:
    q = (question or "").strip()
    a = (answer or "").strip()
    q = q if q else "(no question text provided)"
    a = a if a else "(no response)"
    return textwrap.dedent(
        f"""
        @{asker_login}

        **Question**
        > {q}

        **Answer (gitbot; no code changes made)**
        {a}
        """
    ).strip()


def _format_missing_ticket_summary_answer(*, err: str, review_context_dir: str) -> str:
    """
    When we can't find the per-ticket context markdown, we should be explicit and include the error details.
    """
    e = (err or "").strip() or "(unknown error)"
    return textwrap.dedent(
        f"""
        I could not find the ticket summary (`review_context/*.md`) needed to answer confidently.

        Error:
        {e}

        Expected location:
        {review_context_dir}

        Fix:
        - Re-run bugbot/ticket_runner for this ticket so it writes the `review_context` packet, then mention me again.
        - Or include the ticket key in your comment (e.g. `LAB-1234`) so I can locate the right file.
        """
    ).strip()

def _list_repo_review_comments(
    *,
    auth: GitHubAppAuth,
    owner: str,
    repo: str,
    since_iso: str,
) -> list[CommentEvent]:
    token = auth.get_installation_token()
    url = f"{auth.api_base}/repos/{owner}/{repo}/pulls/comments?since={urllib.parse.quote(since_iso)}&per_page=100"
    data = _gh_api_json_paginated(method="GET", url=url, headers=auth._inst_headers(token))
    out: list[CommentEvent] = []
    for x in data:
        if not isinstance(x, dict):
            continue
        out.append(
            CommentEvent(
                kind="review",
                comment_id=int(x.get("id") or 0),
                body=str(x.get("body") or ""),
                created_at=str(x.get("created_at") or ""),
                user_login=str((x.get("user") or {}).get("login") or ""),
                html_url=str(x.get("html_url") or ""),
                file_path=str(x.get("path") or ""),
                line=int(x.get("line") or 0),
                diff_hunk=str(x.get("diff_hunk") or ""),
                pull_request_url=str(x.get("pull_request_url") or ""),
                issue_url="",
            )
        )
    return [e for e in out if e.comment_id]


def _list_repo_issue_comments(
    *,
    auth: GitHubAppAuth,
    owner: str,
    repo: str,
    since_iso: str,
) -> list[CommentEvent]:
    token = auth.get_installation_token()
    url = f"{auth.api_base}/repos/{owner}/{repo}/issues/comments?since={urllib.parse.quote(since_iso)}&per_page=100"
    data = _gh_api_json_paginated(method="GET", url=url, headers=auth._inst_headers(token))
    out: list[CommentEvent] = []
    for x in data:
        if not isinstance(x, dict):
            continue
        out.append(
            CommentEvent(
                kind="issue",
                comment_id=int(x.get("id") or 0),
                body=str(x.get("body") or ""),
                created_at=str(x.get("created_at") or ""),
                user_login=str((x.get("user") or {}).get("login") or ""),
                html_url=str(x.get("html_url") or ""),
                file_path="",
                line=0,
                diff_hunk="",
                pull_request_url="",
                issue_url=str(x.get("issue_url") or ""),
            )
        )
    return [e for e in out if e.comment_id]


def _openssl_sign_rs256(private_key_path: str, signing_input: bytes) -> bytes:
    """
    Uses openssl for RS256 signing to avoid external Python deps.
    """
    cmd = ["openssl", "dgst", "-sha256", "-sign", private_key_path]
    try:
        p = subprocess.run(cmd, input=signing_input, capture_output=True, check=False)
    except FileNotFoundError:
        raise RuntimeError("openssl not found on PATH; required for GitHub App JWT signing.")
    if p.returncode != 0:
        err = (p.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"openssl signing failed (code {p.returncode}): {err}")
    return p.stdout or b""


def _build_github_app_jwt(*, app_id: str, private_key_path: str) -> str:
    iat = _now_ts() - 60
    exp = _now_ts() + 9 * 60  # GitHub: JWT max 10 minutes
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {"iat": iat, "exp": exp, "iss": app_id}
    signing_input = (_json_b64url(header) + "." + _json_b64url(payload)).encode("ascii")
    sig = _openssl_sign_rs256(private_key_path, signing_input)
    token = signing_input.decode("ascii") + "." + _b64url(sig)
    return token


@dataclass
class GitHubAppAuth:
    app_id: str
    installation_id: str
    private_key_path: str
    api_base: str = GITHUB_API_BASE

    _cached_token: Optional[str] = None
    _cached_expiry_ts: int = 0

    def _app_headers(self, jwt: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "gitbot-local",
        }

    def _inst_headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "gitbot-local",
        }

    def get_installation_token(self) -> str:
        # Refresh if token is missing or close to expiry
        if self._cached_token and (_now_ts() < self._cached_expiry_ts - 60):
            return self._cached_token

        jwt = _build_github_app_jwt(app_id=self.app_id, private_key_path=self.private_key_path)
        url = f"{self.api_base}/app/installations/{self.installation_id}/access_tokens"
        status, _headers, data, text = _http_json(method="POST", url=url, headers=self._app_headers(jwt), payload={})
        if status < 200 or status >= 300 or not isinstance(data, dict) or not data.get("token"):
            raise RuntimeError(
                f"Failed to create installation token (HTTP {status}). Response: {text[:2000]}"
            )
        token = str(data["token"])
        expires_at = str(data.get("expires_at") or "")
        expiry_ts = 0
        if expires_at:
            try:
                # Example: 2020-01-01T00:00:00Z
                dt = datetime.datetime.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
                expiry_ts = int(dt.timestamp())
            except Exception:
                expiry_ts = 0
        self._cached_token = token
        self._cached_expiry_ts = expiry_ts or (_now_ts() + 50 * 60)
        return token


def _private_key_to_path(*, private_key_path: str, private_key_pem_env: str) -> str:
    if private_key_path:
        return private_key_path
    pem = (private_key_pem_env or "").strip()
    if not pem:
        raise RuntimeError("Missing GitHub App private key. Set GITHUB_APP_PRIVATE_KEY_PATH or GITHUB_APP_PRIVATE_KEY.")
    fd, path = tempfile.mkstemp(prefix="gitbot_key_", suffix=".pem")
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(pem)
        if not pem.endswith("\n"):
            f.write("\n")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Local GitHub bot that replies to @gitbot comments (no code changes).")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be posted, but do not post replies.")
    parser.add_argument("--once", action="store_true", help="Run one polling iteration and exit.")
    parser.add_argument(
        "--hello-world",
        action="store_true",
        help="Reply with exactly 'hello world' to any @mention, for end-to-end testing (skips review_context/cursor-agent).",
    )
    parser.add_argument(
        "--bot-login",
        default=os.getenv("GITBOT_BOT_LOGIN", "").strip(),
        help="Optional GitHub login for the bot; used to avoid replying to itself.",
    )

    # Hardcoded defaults (you requested not to provide these each time).
    # You can still override via CLI/env if you ever need to test against a fork.
    parser.add_argument(
        "--owner",
        default=os.getenv("GITHUB_OWNER", DEFAULT_GITHUB_OWNER).strip(),
        help=f"GitHub org/user (default {DEFAULT_GITHUB_OWNER}).",
    )
    parser.add_argument(
        "--repo",
        default=os.getenv("GITHUB_REPO", DEFAULT_GITHUB_REPO).strip(),
        help=f"GitHub repo name (default {DEFAULT_GITHUB_REPO}).",
    )

    parser.add_argument("--app-id", default=os.getenv("GITHUB_APP_ID", "").strip(), help="GitHub App ID.")
    parser.add_argument(
        "--installation-id",
        default=os.getenv("GITHUB_INSTALLATION_ID", "").strip(),
        help="GitHub App installation ID.",
    )
    parser.add_argument(
        "--private-key-path",
        default=os.getenv("GITHUB_APP_PRIVATE_KEY_PATH", "").strip(),
        help="Path to GitHub App private key PEM.",
    )
    parser.add_argument(
        "--db-path",
        default=os.getenv("GITBOT_DB_PATH", DEFAULT_DB_PATH),
        help=f"SQLite DB path (default: {DEFAULT_DB_PATH}).",
    )
    parser.add_argument(
        "--review-context-dir",
        default=os.getenv("REVIEW_CONTEXT_DIR", DEFAULT_REVIEW_CONTEXT_DIR),
        help=f"Directory containing review_context markdown (default: {DEFAULT_REVIEW_CONTEXT_DIR}).",
    )
    parser.add_argument(
        "--repo-dir",
        default=os.getenv("LABGURU_REPO_DIR", DEFAULT_REPO_DIR),
        help=f"Labguru repo dir for local code excerpts (default: {DEFAULT_REPO_DIR}).",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=float(os.getenv("GITBOT_POLL_SECONDS", "45")),
        help="Polling interval seconds (default 45).",
    )
    parser.add_argument(
        "--lookback-hours",
        type=float,
        default=float(os.getenv("GITBOT_LOOKBACK_HOURS", "24")),
        help="Lookback window for first run if DB has no cursor (default 24h).",
    )
    parser.add_argument(
        "--bot-mention",
        default=os.getenv("GITBOT_MENTION", "@gitbot"),
        help="Mention token to trigger replies (default @gitbot).",
    )
    parser.add_argument(
        "--project-prefix",
        default=os.getenv("PROJECT_PREFIX", "LAB"),
        help="Ticket prefix used for inference (default LAB).",
    )
    parser.add_argument(
        "--api-base",
        default=os.getenv("GITHUB_API_BASE", GITHUB_API_BASE),
        help=f"GitHub API base (default {GITHUB_API_BASE}).",
    )
    parser.add_argument(
        "--cursor-bin",
        default=os.getenv("CURSOR_BIN", "cursor-agent"),
        help="cursor-agent binary (default cursor-agent).",
    )
    parser.add_argument(
        "--cursor-model",
        default=os.getenv("CURSOR_MODEL", "gpt-5"),
        help="cursor-agent model (default gpt-5).",
    )
    parser.add_argument(
        "--cursor-timeout-seconds",
        type=float,
        default=float(os.getenv("CURSOR_TIMEOUT_SECONDS", "90")),
        help="cursor-agent timeout seconds (default 90).",
    )

    args = parser.parse_args()
    hello_world_mode = bool(args.hello_world or (os.getenv("GITBOT_HELLO_WORLD", "0").strip() == "1"))

    if not args.owner or not args.repo:
        raise SystemExit("Missing repo owner/name (unexpected).")
    if not args.app_id or not args.installation_id:
        raise SystemExit("Missing GitHub App auth. Set --app-id and --installation-id (or env GITHUB_APP_ID/GITHUB_INSTALLATION_ID).")

    key_path = _private_key_to_path(
        private_key_path=args.private_key_path,
        private_key_pem_env=os.getenv("GITHUB_APP_PRIVATE_KEY", ""),
    )
    auth = GitHubAppAuth(
        app_id=args.app_id,
        installation_id=args.installation_id,
        private_key_path=key_path,
        api_base=args.api_base,
    )

    db = StateDB(args.db_path)
    cursor_key = f"cursor:{args.owner}/{args.repo}"
    mention_token = (args.bot_mention or "@gitbot").strip()

    try:
        while True:
            cursor = db.get_cursor_iso(cursor_key)
            if not cursor:
                since_dt = datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(hours=float(args.lookback_hours))
                cursor = _dt_to_cursor_iso(since_dt)
            _log(f"Polling since {cursor} ...")

            review_comments = _list_repo_review_comments(auth=auth, owner=args.owner, repo=args.repo, since_iso=cursor)
            issue_comments = _list_repo_issue_comments(auth=auth, owner=args.owner, repo=args.repo, since_iso=cursor)

            events = review_comments + issue_comments
            events.sort(key=lambda e: (_iso_to_dt(e.created_at), e.kind, e.comment_id))
            _log(f"Fetched {len(review_comments)} review comments, {len(issue_comments)} issue comments.")

            actionable = 0
            for ev in events:
                if not ev.created_at:
                    continue
                if args.bot_login and ev.user_login and ev.user_login.lower() == args.bot_login.lower():
                    db.set_cursor_iso(cursor_key, ev.created_at)
                    continue

                if db.is_processed(platform="github", comment_kind=ev.kind, comment_id=ev.comment_id):
                    db.set_cursor_iso(cursor_key, ev.created_at)
                    continue
                if not _mention_present(ev.body, mention_token):
                    db.set_cursor_iso(cursor_key, ev.created_at)
                    continue

                actionable += 1
                _log(f"Actionable @{mention_token} mention in {ev.kind} comment {ev.comment_id}: {ev.html_url}")
                # Hello-world mode: pure transport test (skip review_context/cursor-agent).
                if hello_world_mode:
                    question = _strip_bot_mention_and_ticket(body=ev.body, mention_token=mention_token, ticket_key="") or ev.body
                    ticket_key = (args.project_prefix or "LAB").strip().upper() + "-????"
                    pr_num: Optional[int] = None
                    issue_num: Optional[int] = None
                    if ev.kind == "review":
                        pr_num = _parse_pr_number_from_url(ev.pull_request_url)
                    else:
                        issue_num = _parse_issue_number_from_url(ev.issue_url)
                    answer = "hello world"
                else:
                    resolved, err = resolve_context_for_event(
                        auth=auth,
                        owner=args.owner,
                        repo=args.repo,
                        review_context_dir=args.review_context_dir,
                        project_prefix=args.project_prefix,
                        ev=ev,
                    )
                    question = ev.body
                    ticket_key = ""
                    pr_num = None
                    issue_num = None
                    pr_info: Optional[PRInfo] = None

                    if resolved:
                        ticket_key = resolved.ticket_key
                        pr_info = resolved.pr
                        if pr_info:
                            pr_num = pr_info.number
                        question = _strip_bot_mention_and_ticket(
                            body=ev.body,
                            mention_token=mention_token,
                            ticket_key=resolved.ticket_key,
                        ) or ev.body
                    else:
                        # We still reply (to avoid getting stuck), but explain what's missing.
                        ticket_key = (args.project_prefix or "LAB").strip().upper() + "-????"
                        question = _strip_bot_mention_and_ticket(body=ev.body, mention_token=mention_token, ticket_key="") or ev.body

                    if ev.kind == "review":
                        pr_num = pr_num or _parse_pr_number_from_url(ev.pull_request_url)
                    else:
                        issue_num = _parse_issue_number_from_url(ev.issue_url)
                        if pr_num is None and resolved and resolved.pr:
                            pr_num = resolved.pr.number

                    code_excerpt = ""
                    if ev.file_path:
                        code_excerpt = _read_code_excerpt(repo_dir=args.repo_dir, file_path=ev.file_path, line=ev.line)

                    if resolved:
                        prompt = _build_answer_prompt(
                            ticket_key=resolved.ticket_key,
                            question=question or "(no question text provided)",
                            comment_url=ev.html_url,
                            context_md=resolved.context_md,
                            pr=resolved.pr,
                            review_file_path=ev.file_path,
                            review_line=ev.line,
                            diff_hunk=ev.diff_hunk,
                            code_excerpt=code_excerpt,
                        )
                        answer = _run_cursor_agent(
                            prompt=prompt,
                            cursor_bin=args.cursor_bin,
                            model=args.cursor_model,
                            timeout_seconds=float(args.cursor_timeout_seconds),
                        )
                        answer = _ensure_single_trailing_git_mode_line(answer)
                    else:
                        answer = _format_missing_ticket_summary_answer(err=err, review_context_dir=args.review_context_dir)

                reply_body = _format_reply_markdown(
                    asker_login=ev.user_login or "there",
                    question=question,
                    answer=answer,
                )

                if args.dry_run:
                    _log("DRY RUN: would post reply:\n" + reply_body[:1500] + ("\n(TRUNCATED)" if len(reply_body) > 1500 else ""))
                    db.set_cursor_iso(cursor_key, ev.created_at)
                    continue

                try:
                    if ev.kind == "review":
                        if not pr_num:
                            raise RuntimeError("Cannot determine PR number for review comment reply.")
                        reply_url = _post_review_reply(
                            auth=auth,
                            owner=args.owner,
                            repo=args.repo,
                            pr_number=int(pr_num),
                            in_reply_to_comment_id=int(ev.comment_id),
                            body=reply_body,
                        )
                    else:
                        if not issue_num:
                            raise RuntimeError("Cannot determine issue/PR number for issue comment reply.")
                        reply_url = _post_issue_comment(
                            auth=auth,
                            owner=args.owner,
                            repo=args.repo,
                            issue_number=int(issue_num),
                            body=reply_body,
                        )
                    db.mark_processed(
                        platform="github",
                        comment_kind=ev.kind,
                        comment_id=ev.comment_id,
                        repo_owner=args.owner,
                        repo_name=args.repo,
                        created_at=ev.created_at,
                        ticket_key=ticket_key,
                        reply_url=reply_url,
                    )
                    db.set_cursor_iso(cursor_key, ev.created_at)
                    _log(f"Posted reply: {reply_url or '(no url)'}")
                except Exception as e:
                    # Do NOT advance cursor past a failed actionable event.
                    _log(f"Reply failed; will retry later without advancing cursor. Error: {e}")
                    break

            _log(f"Detected {actionable} actionable mention(s).")
            if args.once:
                return 0
            time.sleep(max(1.0, float(args.poll_seconds)))
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())


