from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import PurePosixPath
from typing import Optional

from .logging import _log
from .types import PrCreationError


DEFAULT_UNTRACKED_EXCLUDE_GLOBS = [
    "**/*.md",
]


def _parse_csv_env_list(name: str) -> list[str]:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _matches_any_glob(path: str, patterns: list[str]) -> bool:
    # Git paths are POSIX-style even on macOS.
    p = PurePosixPath(path)
    return any(p.match(pattern) for pattern in patterns)


def _unstage_paths_matching_globs(*, repo_dir: str, globs: list[str]) -> list[str]:
    """
    Remove files from the index (staging area) if their path matches any exclude glob.
    This does NOT touch the working tree.
    Returns the list of unstaged paths.
    """
    if not globs:
        return []
    res = _run_cmd(
        cmd=["git", "diff", "--cached", "--name-only", "-z"],
        cwd=repo_dir,
        capture_output=True,
        check=False,
    )
    staged = [p for p in (res.stdout or "").split("\0") if p]
    to_unstage = [p for p in staged if _matches_any_glob(p, globs)]
    if not to_unstage:
        return []
    for chunk in _chunked(to_unstage, 100):
        _run_cmd(cmd=["git", "reset", "--", *chunk], cwd=repo_dir, check=False, capture_output=True)
    return to_unstage


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


def _ensure_git_repo(repo_dir: str) -> None:
    res = _run_cmd(
        cmd=["git", "rev-parse", "--is-inside-work-tree"],
        cwd=repo_dir,
        capture_output=True,
        check=False,
    )
    if res.returncode != 0 or "true" not in (res.stdout or ""):
        raise RuntimeError(f"Not a git repository: {repo_dir}")


def _git_current_branch(repo_dir: str) -> str:
    res = _run_cmd(
        cmd=["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
    )
    return (res.stdout or "").strip()


def _git_has_tracked_changes(repo_dir: str) -> bool:
    """
    True if there are staged or unstaged tracked changes.
    Ignores untracked files (they don't always prevent checkout).
    """
    unstaged = _run_cmd(
        cmd=["git", "diff", "--quiet"],
        cwd=repo_dir,
        check=False,
        capture_output=False,
    ).returncode
    staged = _run_cmd(
        cmd=["git", "diff", "--cached", "--quiet"],
        cwd=repo_dir,
        check=False,
        capture_output=False,
    ).returncode
    return unstaged != 0 or staged != 0


def _find_branch_containing_issue_key(repo_dir: str, issue_key: str) -> Optional[str]:
    """
    Find the most recently updated local branch whose name includes the issue key.
    Example: bugfix/LAB-26649-something
    """
    issue_key_u = (issue_key or "").upper()
    res = _run_cmd(
        cmd=["git", "for-each-ref", "--sort=-committerdate", "--format=%(refname:short)", "refs/heads"],
        cwd=repo_dir,
        capture_output=True,
        check=False,
    )
    for line in (res.stdout or "").splitlines():
        name = line.strip()
        if not name:
            continue
        if issue_key_u in name.upper():
            return name
    return None


def _find_remote_branch_containing_issue_key(repo_dir: str, issue_key: str) -> Optional[str]:
    issue_key_u = (issue_key or "").upper()
    res = _run_cmd(
        cmd=[
            "git",
            "for-each-ref",
            "--sort=-committerdate",
            "--format=%(refname:short)",
            "refs/remotes/origin",
        ],
        cwd=repo_dir,
        capture_output=True,
        check=False,
    )
    for line in (res.stdout or "").splitlines():
        name = line.strip()
        if not name:
            continue
        if issue_key_u in name.upper():
            return name
    return None


def run_karamba_new_in_repo(*, issue_key: str, repo_dir: str) -> None:
    subprocess.run(["karamba", "new", issue_key], check=True, cwd=repo_dir)


def _ensure_on_ticket_branch(*, repo_dir: str, issue_key: str) -> str:
    """
    Ensure we are on a branch for the given ticket, regardless of current branch.
    Strategy:
    - If already on a branch containing issue_key => do nothing
    - Else if a local branch name contains issue_key => checkout it
    - Else if a remote origin/* branch contains issue_key => checkout -B local tracking branch
    - Else fall back to `karamba new <issue_key>` (creates + checks out a new branch)
    """
    _ensure_git_repo(repo_dir)
    current = _git_current_branch(repo_dir)
    if current and issue_key.upper() in current.upper():
        return current

    local = _find_branch_containing_issue_key(repo_dir, issue_key)
    if local:
        _log(f"Checking out existing local ticket branch: {local}")
        _run_cmd(cmd=["git", "checkout", local], cwd=repo_dir)
        return local

    remote = _find_remote_branch_containing_issue_key(repo_dir, issue_key)
    if remote and remote.startswith("origin/"):
        local_name = remote[len("origin/") :]
        _log(f"Checking out ticket branch from remote: {remote} -> {local_name}")
        _run_cmd(cmd=["git", "checkout", "-B", local_name, remote], cwd=repo_dir)
        return local_name

    _log(
        f"Could not find an existing local/remote branch containing {issue_key}. "
        "Falling back to `karamba new` to create the ticket branch."
    )
    run_karamba_new_in_repo(issue_key=issue_key, repo_dir=repo_dir)
    return _git_current_branch(repo_dir)


def _has_tracked_changes(repo_dir: str) -> bool:
    # Unstaged tracked changes
    if _run_cmd(cmd=["git", "diff", "--quiet"], cwd=repo_dir, check=False).returncode != 0:
        return True
    # Staged tracked changes
    if _run_cmd(cmd=["git", "diff", "--cached", "--quiet"], cwd=repo_dir, check=False).returncode != 0:
        return True
    return False


def _stash_tracked_changes(*, repo_dir: str, message: str) -> bool:
    """
    Stashes tracked changes (including staged). Does NOT include untracked files.
    Returns True if a stash was created.
    """
    if not _has_tracked_changes(repo_dir):
        return False
    _run_cmd(
        cmd=["git", "stash", "push", "-m", message],
        cwd=repo_dir,
        check=False,
        capture_output=True,
    )
    return True


def _pop_stash_best_effort(*, repo_dir: str) -> None:
    """
    Best-effort: pop the most recent stash. If it fails (conflicts), keep the stash and warn.
    """
    res = _run_cmd(
        cmd=["git", "stash", "pop"],
        cwd=repo_dir,
        check=False,
        capture_output=True,
    )
    if res.returncode != 0:
        out = ((res.stdout or "") + "\n" + (res.stderr or "")).strip()
        _log("WARNING: failed to apply stash cleanly; leaving stash in place.")
        if out:
            _log(out)


def _read_karamba_env(repo_dir: str) -> dict[str, str]:
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
        if len(val) >= 2 and ((val[0] == val[-1] == '"') or (val[0] == val[-1] == "'")):
            val = val[1:-1]
        env[key] = val
    return env


def _git_origin_head_branch(repo_dir: str) -> str:
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


def _resolve_base_ref_for_redo_pr(repo_dir: str) -> str:
    """
    Decide the base ref to reset the ticket branch to for --redo-pr "clean slate".
    Default: origin/<STAGING_BRANCH> (git_flow), else origin/<MAIN_BRANCH>, else origin/HEAD-derived.
    """
    cfg = _read_karamba_env(repo_dir)
    base_branch = (cfg.get("STAGING_BRANCH") or "").strip() or (cfg.get("MAIN_BRANCH") or "").strip()
    if not base_branch:
        base_branch = _git_origin_head_branch(repo_dir)
    # Prefer origin/<branch> if it exists; else fall back to local <branch>.
    origin_ref = f"refs/remotes/origin/{base_branch}"
    if (
        _run_cmd(cmd=["git", "show-ref", "--verify", "--quiet", origin_ref], cwd=repo_dir, check=False).returncode
        == 0
    ):
        return f"origin/{base_branch}"
    return base_branch


def redo_pr_prepare_clean_slate(*, repo_dir: str, issue_key: str) -> tuple[str, str, bool]:
    """
    Implements --redo-pr "clean slate":
    - stash tracked changes on the *current* branch (to avoid losing work)
    - checkout ticket branch
    - hard reset ticket branch to base (typically origin/staging) and remove untracked files
    Returns (original_branch, ticket_branch, stashed_bool).
    """
    _ensure_git_repo(repo_dir)
    original_branch = _git_current_branch(repo_dir) or ""
    stash_msg = f"bugbot-auto-stash {issue_key} {int(time.time())}"
    stashed = _stash_tracked_changes(repo_dir=repo_dir, message=stash_msg)
    if stashed:
        _log("Stashed local tracked changes (will restore at end).")

    ticket_branch = _ensure_on_ticket_branch(repo_dir=repo_dir, issue_key=issue_key)
    base_ref = _resolve_base_ref_for_redo_pr(repo_dir)
    _log(f"redo-pr clean slate: resetting {ticket_branch} to {base_ref} and cleaning untracked files...")
    _run_cmd(cmd=["git", "reset", "--hard", base_ref], cwd=repo_dir, check=False, capture_output=True)
    _run_cmd(cmd=["git", "clean", "-fd"], cwd=repo_dir, check=False, capture_output=True)
    return original_branch, ticket_branch, stashed


def redo_pr_restore_stash(*, repo_dir: str, original_branch: str, ticket_branch: str, stashed: bool) -> None:
    """
    Best-effort restore for redo_pr_prepare_clean_slate().
    Restores stash on the original branch (where it was created), then attempts to return to ticket branch.
    """
    if not stashed:
        return
    if original_branch and original_branch != _git_current_branch(repo_dir):
        _run_cmd(cmd=["git", "checkout", original_branch], cwd=repo_dir, check=False, capture_output=True)
    _pop_stash_best_effort(repo_dir=repo_dir)
    if ticket_branch and ticket_branch != original_branch:
        # Try to return to the ticket branch, but don't fail the whole run if local changes prevent it.
        res = _run_cmd(cmd=["git", "checkout", ticket_branch], cwd=repo_dir, check=False, capture_output=True)
        if res.returncode != 0:
            _log("WARNING: could not checkout ticket branch after restoring stash; leaving you on current branch.")


def _sanitize_commit_message(raw: str) -> str:
    # Git commit subject should be a single line; keep it robust against accidental newlines/ANSI.
    msg = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw or "")  # strip common ANSI escapes
    msg = msg.replace("\r", "\n").split("\n", 1)[0].strip()
    if not msg:
        raise RuntimeError("Commit message resolved to empty after sanitization.")
    return msg


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


def run_git_commit_push_and_pr(
    *,
    repo_dir: str,
    issue_key: str,
    commit_name_from_cursor: str,
    preexisting_untracked: Optional[set[str]] = None,
    create_pr: bool = True,
    exclude_untracked_globs: Optional[list[str]] = None,
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
            # Avoid staging some untracked files that BugBot commonly generates but are usually not
            # ticket-related (e.g. markdown packets/spec notes). This is independent of repo .gitignore.
            effective_excludes = (
                exclude_untracked_globs
                if exclude_untracked_globs is not None
                else (
                    DEFAULT_UNTRACKED_EXCLUDE_GLOBS
                    + _parse_csv_env_list("BUGBOT_UNTRACKED_EXCLUDE_GLOBS")
                )
            )
            filtered = (
                [p for p in new_untracked if not _matches_any_glob(p, effective_excludes)]
                if effective_excludes
                else new_untracked
            )
            skipped = sorted(set(new_untracked) - set(filtered))
            if skipped:
                _log(
                    "Skipping staging of some newly-created untracked files due to exclude globs. "
                    f"Skipped {len(skipped)} file(s)."
                )
            _log(f"Staging {len(filtered)} newly created file(s) from this run...")
            for chunk in _chunked(filtered, 100):
                _run_cmd(cmd=["git", "add", "--", *chunk], cwd=repo_dir)

    # Final safety: even if a markdown file is tracked (from older commits), we generally don't want
    # BugBot to include markdown changes in the commit. Unstage them here, independent of .gitignore.
    unstaged = _unstage_paths_matching_globs(repo_dir=repo_dir, globs=DEFAULT_UNTRACKED_EXCLUDE_GLOBS)
    if unstaged:
        _log(f"Unstaged {len(unstaged)} file(s) matching exclude globs (e.g. *.md).")

    # If nothing is staged, `git commit` will fail with messages like:
    # "nothing added to commit but untracked files present".
    # This is expected when Cursor produced no changes and we intentionally avoid staging
    # pre-existing untracked files. Treat it as a non-error and continue to push/PR.
    staged_is_clean = (
        _run_cmd(cmd=["git", "diff", "--cached", "--quiet"], cwd=repo_dir, check=False).returncode == 0
    )
    if staged_is_clean:
        _log("No staged changes to commit. Continuing to push/PR.")
    else:
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
            combined = (stderr + "\n" + stdout).lower()
            # Common "nothing to commit" cases: allow continuing to push/pr.
            if ("nothing to commit" in combined) or ("nothing added to commit" in combined):
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


