from __future__ import annotations

import datetime
import os
import re
import textwrap
from typing import Optional

from .constants import (
    MAX_REVIEW_CONTEXT_DESCRIPTION_CHARS,
    MAX_REVIEW_CONTEXT_DIFF_CHARS,
    REVIEW_CONTEXT_DIR,
)
from .git_ops import _git_current_branch, _run_cmd
from .types import JiraIssue, RunnerOutput


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

    has_origin = (
        _run_cmd(
            cmd=["git", "show-ref", "--verify", "--quiet", origin_ref],
            cwd=repo_dir,
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )
    if has_origin:
        return f"origin/{base_branch}", src

    has_local = (
        _run_cmd(
            cmd=["git", "show-ref", "--verify", "--quiet", local_ref],
            cwd=repo_dir,
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )
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
    issue: JiraIssue,
    runner_output: RunnerOutput,
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

    description = _truncate(
        issue.description_text or "(No Jira description provided)",
        MAX_REVIEW_CONTEXT_DESCRIPTION_CHARS,
    )
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


