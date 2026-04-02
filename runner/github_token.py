from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional


DEFAULT_GITHUB_API_BASE = "https://api.github.com"


@dataclass(frozen=True)
class GitHubPullRequest:
    number: int
    html_url: str
    title: str
    body: str
    head_ref: str
    head_sha: str
    head_repo_full_name: str
    base_ref: str
    base_sha: str
    base_repo_full_name: str


@dataclass(frozen=True)
class GitHubComment:
    kind: str
    comment_id: int
    html_url: str
    body: str
    created_at: str
    user_login: str
    in_reply_to_id: int = 0
    file_path: str = ""
    line: int = 0
    diff_hunk: str = ""


def _github_headers(token: str) -> dict[str, str]:
    tok = (token or "").strip()
    if not tok:
        raise RuntimeError("Missing GitHub credentials (GITHUB_TOKEN).")
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {tok}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "BugBot-git-mode",
    }


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


def _http_get_json(*, url: str, headers: dict[str, str], timeout: float = 30.0) -> Any:
    status, _resp_headers, raw = _http_request(method="GET", url=url, headers=headers, timeout=timeout)
    text = raw.decode("utf-8", errors="replace")
    if status < 200 or status >= 300:
        raise RuntimeError(f"GitHub API GET failed (HTTP {status}) for {url}: {text[:2000]}")
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except Exception as e:
        raise RuntimeError(f"GitHub API returned non-JSON for {url}: {text[:2000]}") from e


def _http_get_text(*, url: str, headers: dict[str, str], timeout: float = 90.0) -> str:
    status, _resp_headers, raw = _http_request(method="GET", url=url, headers=headers, timeout=timeout)
    text = raw.decode("utf-8", errors="replace")
    if status < 200 or status >= 300:
        raise RuntimeError(f"GitHub API GET failed (HTTP {status}) for {url}: {text[:2000]}")
    return text


def _parse_link_header(link_value: str) -> dict[str, str]:
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
        status, resp_headers, raw = _http_request(method="GET", url=next_url, headers=headers, timeout=timeout)
        text = raw.decode("utf-8", errors="replace")
        if status < 200 or status >= 300:
            raise RuntimeError(f"GitHub API GET failed (HTTP {status}) for {next_url}: {text[:2000]}")
        data: Any
        try:
            data = json.loads(text) if text.strip() else None
        except Exception:
            raise RuntimeError(f"GitHub API returned non-JSON for {next_url}: {text[:2000]}")
        if isinstance(data, list):
            items.extend(data)
        elif data is None:
            break
        else:
            raise RuntimeError(f"Unexpected GitHub API response shape for {next_url}: {type(data).__name__}")
        link = _parse_link_header(resp_headers.get("link", ""))
        next_url = link.get("next")
    return items


def parse_pr_number_from_url(pr_url: str) -> Optional[int]:
    m = re.search(r"https?://github\.com/[^/\s]+/[^/\s]+/pull/(\d+)\b", pr_url or "", re.IGNORECASE)
    return int(m.group(1)) if m else None


def search_pr_number_for_ticket(*, api_base: str, token: str, repo: str, ticket_key: str) -> Optional[int]:
    r = (repo or "").strip()
    t = (ticket_key or "").strip()
    if not r or not t:
        return None
    q = urllib.parse.quote(f"repo:{r} is:pr {t}")
    url = f"{(api_base or DEFAULT_GITHUB_API_BASE).rstrip('/')}/search/issues?q={q}&sort=updated&order=desc"
    data = _http_get_json(url=url, headers=_github_headers(token), timeout=30)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    for it in items[:5]:
        if not isinstance(it, dict):
            continue
        num = it.get("number")
        if isinstance(num, int):
            return num
    return None


def fetch_pull_request(*, api_base: str, token: str, repo: str, pr_number: int) -> GitHubPullRequest:
    url = f"{(api_base or DEFAULT_GITHUB_API_BASE).rstrip('/')}/repos/{repo}/pulls/{int(pr_number)}"
    data = _http_get_json(url=url, headers=_github_headers(token), timeout=30)
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected PR payload for #{pr_number}: {type(data).__name__}")
    head = data.get("head") if isinstance(data.get("head"), dict) else {}
    base = data.get("base") if isinstance(data.get("base"), dict) else {}
    head_repo = head.get("repo") if isinstance(head.get("repo"), dict) else {}
    base_repo = base.get("repo") if isinstance(base.get("repo"), dict) else {}
    return GitHubPullRequest(
        number=int(data.get("number") or pr_number),
        html_url=str(data.get("html_url") or "").strip(),
        title=str(data.get("title") or "").strip(),
        body=str(data.get("body") or ""),
        head_ref=str((head or {}).get("ref") or "").strip(),
        head_sha=str((head or {}).get("sha") or "").strip(),
        head_repo_full_name=str((head_repo or {}).get("full_name") or "").strip(),
        base_ref=str((base or {}).get("ref") or "").strip(),
        base_sha=str((base or {}).get("sha") or "").strip(),
        base_repo_full_name=str((base_repo or {}).get("full_name") or "").strip(),
    )


def fetch_pull_request_diff(*, api_base: str, token: str, repo: str, pr_number: int) -> str:
    url = f"{(api_base or DEFAULT_GITHUB_API_BASE).rstrip('/')}/repos/{repo}/pulls/{int(pr_number)}"
    headers = dict(_github_headers(token))
    headers["Accept"] = "application/vnd.github.v3.diff"
    return _http_get_text(url=url, headers=headers, timeout=120.0)


def list_pull_request_review_comments(
    *,
    api_base: str,
    token: str,
    repo: str,
    pr_number: int,
) -> list[GitHubComment]:
    url = f"{(api_base or DEFAULT_GITHUB_API_BASE).rstrip('/')}/repos/{repo}/pulls/{int(pr_number)}/comments?per_page=100"
    data = _gh_api_json_paginated(url=url, headers=_github_headers(token), timeout=30, max_pages=30)
    out: list[GitHubComment] = []
    for x in data:
        if not isinstance(x, dict):
            continue
        out.append(
            GitHubComment(
                kind="review",
                comment_id=int(x.get("id") or 0),
                html_url=str(x.get("html_url") or ""),
                body=str(x.get("body") or ""),
                created_at=str(x.get("created_at") or ""),
                user_login=str((x.get("user") or {}).get("login") or ""),
                in_reply_to_id=int(x.get("in_reply_to_id") or 0),
                file_path=str(x.get("path") or ""),
                line=int(x.get("line") or 0),
                diff_hunk=str(x.get("diff_hunk") or ""),
            )
        )
    return [c for c in out if c.comment_id]


def list_pull_request_issue_comments(
    *,
    api_base: str,
    token: str,
    repo: str,
    pr_number: int,
) -> list[GitHubComment]:
    url = f"{(api_base or DEFAULT_GITHUB_API_BASE).rstrip('/')}/repos/{repo}/issues/{int(pr_number)}/comments?per_page=100"
    data = _gh_api_json_paginated(url=url, headers=_github_headers(token), timeout=30, max_pages=30)
    out: list[GitHubComment] = []
    for x in data:
        if not isinstance(x, dict):
            continue
        out.append(
            GitHubComment(
                kind="issue",
                comment_id=int(x.get("id") or 0),
                html_url=str(x.get("html_url") or ""),
                body=str(x.get("body") or ""),
                created_at=str(x.get("created_at") or ""),
                user_login=str((x.get("user") or {}).get("login") or ""),
            )
        )
    return [c for c in out if c.comment_id]


