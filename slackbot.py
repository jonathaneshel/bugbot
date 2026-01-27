#!/usr/bin/env python3
from __future__ import annotations

import logging
import json
import base64
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Optional

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler


BUGBOT_FILES_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_REVIEW_CONTEXT_DIR = os.path.join(BUGBOT_FILES_DIR, "review_context")
DEFAULT_LABGURU_REPO_DIR = "/Users/jonathaneshel/Desktop/Code/Labguru"
DEFAULT_JIRA_BASE_URL = "https://labguru.atlassian.net"
DEFAULT_GITHUB_REPO = "BioData/Labguru"

REVIEW_CONTEXT_DIR = os.getenv("REVIEW_CONTEXT_DIR", DEFAULT_REVIEW_CONTEXT_DIR)
PROJECT_PREFIX = (os.getenv("PROJECT_PREFIX", "LAB") or "LAB").strip().upper()

CURSOR_BIN = os.getenv("CURSOR_BIN", "cursor-agent")
CURSOR_MODEL = os.getenv("CURSOR_MODEL", "gpt-5.2-high")
CURSOR_TIMEOUT_SECONDS = float(os.getenv("CURSOR_TIMEOUT_SECONDS", "240"))
CURSOR_RETRIES = int(os.getenv("CURSOR_RETRIES", "2"))
CURSOR_RETRY_BACKOFF_SECONDS = float(os.getenv("CURSOR_RETRY_BACKOFF_SECONDS", "1.5"))

SLACK_MAX_CHARS = int(os.getenv("SLACK_MAX_CHARS", "3500"))
WORKERS = int(os.getenv("SLACKBOT_WORKERS", "4"))

LABGURU_REPO_DIR = os.getenv("LABGURU_REPO_DIR", DEFAULT_LABGURU_REPO_DIR)
DEFAULT_LABGURU_MAIN_WORKTREE_DIR = os.path.join(BUGBOT_FILES_DIR, ".labguru_main_worktree")
LABGURU_MAIN_WORKTREE_DIR = os.getenv("LABGURU_MAIN_WORKTREE_DIR", DEFAULT_LABGURU_MAIN_WORKTREE_DIR)
LABGURU_MAIN_REF = os.getenv("LABGURU_MAIN_REF", "origin/main")

DEFAULT_SESSIONS_PATH = os.path.join(BUGBOT_FILES_DIR, ".slackbot_sessions.json")
SESSIONS_PATH = os.getenv("SLACKBOT_SESSIONS_PATH", DEFAULT_SESSIONS_PATH)
MAX_TURNS = int(os.getenv("SLACKBOT_MAX_TURNS", "50"))

SLOW_UPDATE_SECONDS = float(os.getenv("SLACKBOT_SLOW_UPDATE_SECONDS", "30"))
SLOW_UPDATE_TEXT = os.getenv("SLACKBOT_SLOW_UPDATE_TEXT", "Still working…")

JIRA_BASE_URL = os.getenv("JIRA_BASE_URL", DEFAULT_JIRA_BASE_URL)
JIRA_EMAIL = (os.getenv("JIRA_EMAIL", "") or "").strip()
JIRA_API_TOKEN = (os.getenv("JIRA_API_TOKEN", "") or "").strip()

GITHUB_TOKEN = (os.getenv("GITHUB_TOKEN", "") or "").strip()
GITHUB_REPO = (os.getenv("GITHUB_REPO", DEFAULT_GITHUB_REPO) or DEFAULT_GITHUB_REPO).strip()

DROP_EXACT_LINES = {"INSTRUCTIONS RECIEVED", "STARTED READING"}

_EXECUTOR = ThreadPoolExecutor(max_workers=WORKERS)


@dataclass(frozen=True)
class ParsedRequest:
    ticket_key: str
    question: str


@dataclass
class Session:
    ticket_key: Optional[str]
    github_repo: Optional[str]
    pr_number: Optional[int]
    pr_url: Optional[str]
    history: list[dict[str, str]]  # [{"role": "user"|"assistant", "text": "..."}]
    updated_at_ms: int


class SessionStore:
    def __init__(self, *, path: str, max_turns: int) -> None:
        self._path = os.path.abspath(os.path.expanduser(path))
        self._max_turns = max(1, int(max_turns))
        self._lock = threading.Lock()
        self._loaded = False
        self._data: dict[str, Session] = {}

    def _load_locked(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return
        except Exception:
            return

        sessions = raw.get("sessions") if isinstance(raw, dict) else None
        if not isinstance(sessions, dict):
            return

        for key, val in sessions.items():
            if not isinstance(key, str) or not isinstance(val, dict):
                continue
            ticket_key = val.get("ticket_key")
            if not isinstance(ticket_key, str) or not ticket_key.strip():
                ticket_key = None
            github_repo = val.get("github_repo")
            if not isinstance(github_repo, str) or not github_repo.strip():
                github_repo = None
            pr_number = val.get("pr_number")
            if not isinstance(pr_number, int):
                pr_number = None
            pr_url = val.get("pr_url")
            if not isinstance(pr_url, str) or not pr_url.strip():
                pr_url = None
            history = val.get("history")
            if not isinstance(history, list):
                history = []
            cleaned_history: list[dict[str, str]] = []
            for item in history:
                if not isinstance(item, dict):
                    continue
                role = item.get("role")
                text = item.get("text")
                if role not in ("user", "assistant"):
                    continue
                if not isinstance(text, str) or not text.strip():
                    continue
                cleaned_history.append({"role": role, "text": text.strip()})
            updated_at_ms = val.get("updated_at_ms")
            if not isinstance(updated_at_ms, int):
                updated_at_ms = int(time.time() * 1000)

            # Cap on load
            if len(cleaned_history) > self._max_turns:
                cleaned_history = cleaned_history[-self._max_turns :]

            self._data[key] = Session(
                ticket_key=ticket_key,
                github_repo=github_repo,
                pr_number=pr_number,
                pr_url=pr_url,
                history=cleaned_history,
                updated_at_ms=updated_at_ms,
            )

    def get(self, key: str) -> Session:
        with self._lock:
            self._load_locked()
            sess = self._data.get(key)
            if sess is None:
                sess = Session(
                    ticket_key=None,
                    github_repo=None,
                    pr_number=None,
                    pr_url=None,
                    history=[],
                    updated_at_ms=int(time.time() * 1000),
                )
                self._data[key] = sess
            return sess

    def save(self) -> None:
        with self._lock:
            self._load_locked()
            parent = os.path.dirname(self._path)
            if parent:
                os.makedirs(parent, exist_ok=True)

            payload = {
                "version": 1,
                "sessions": {
                    k: {
                        "ticket_key": v.ticket_key,
                        "github_repo": v.github_repo,
                        "pr_number": v.pr_number,
                        "pr_url": v.pr_url,
                        "history": v.history,
                        "updated_at_ms": v.updated_at_ms,
                    }
                    for k, v in self._data.items()
                },
            }

            fd, tmp_path = tempfile.mkstemp(prefix=".slackbot_sessions_", suffix=".json", dir=parent or None)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False)
                    f.write("\n")
                os.replace(tmp_path, self._path)
            finally:
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    pass


def _strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", s or "")

def _log_banner(log: logging.Logger, message: str) -> None:
    """
    Print a very visible log line. Uses ANSI bold so it stands out in terminals.
    """
    try:
        log.info("\033[1m%s\033[0m", message)
    except Exception:
        try:
            log.info("%s", message)
        except Exception:
            pass

LOG_MESSAGE_CONTENT = os.getenv("SLACKBOT_LOG_MESSAGE_CONTENT", "0").strip() == "1"
LOG_CONTENT_MAX_CHARS = int(os.getenv("SLACKBOT_LOG_CONTENT_MAX_CHARS", "800"))

def _redact_secrets(text: str) -> str:
    """
    Best-effort redaction for common token formats.
    """
    s = text or ""
    # Slack tokens
    s = re.sub(r"\bxox[baprs]-[A-Za-z0-9-]+\b", "[REDACTED_SLACK_TOKEN]", s)
    s = re.sub(r"\bxapp-[A-Za-z0-9-]+\b", "[REDACTED_SLACK_TOKEN]", s)
    # GitHub tokens (classic + fine-grained)
    s = re.sub(r"\bgh[pousr]_[A-Za-z0-9_]+\b", "[REDACTED_GITHUB_TOKEN]", s)
    # Generic bearer token patterns
    s = re.sub(r"(?i)\b(bearer)\s+[A-Za-z0-9._-]+\b", r"\1 [REDACTED_TOKEN]", s)
    return s

def _truncate_for_log(text: str, max_chars: int) -> str:
    s = (text or "").replace("\r\n", "\n")
    if max_chars <= 0:
        return ""
    if len(s) <= max_chars:
        return s
    return s[:max_chars].rstrip() + "…(truncated)"

def _log_content(log: logging.Logger, label: str, text: str) -> None:
    if not LOG_MESSAGE_CONTENT:
        return
    safe = _truncate_for_log(_redact_secrets(text), LOG_CONTENT_MAX_CHARS)
    _log_banner(log, f"{label}: {safe}")

def _extract_first_json_object(text: str) -> Optional[str]:
    """
    Best-effort extraction of the first JSON object from LLM output.
    This is NOT for interpreting user intent; it's only to parse the interpreter's response.
    """
    if not text:
        return None
    s = text.strip()
    if s.startswith("{") and s.endswith("}"):
        return s
    m = re.search(r"\{[\s\S]*\}", s)
    return m.group(0).strip() if m else None

def _cursor_interpreter_prompt(*, raw_text: str, session: Session, is_app_mention: bool) -> str:
    """
    Returns a prompt that asks cursor-agent to output strict JSON only.
    """
    raw = raw_text or ""
    cleaned = raw
    if is_app_mention:
        cleaned = _remove_leading_mentions(raw)

    session_state = {
        "ticket_key": session.ticket_key,
        "pr_url": session.pr_url,
        "pr_number": session.pr_number,
        "github_repo": session.github_repo or GITHUB_REPO,
    }
    # Keep transcript short for interpretation.
    prior = session.history[-10:] if session.history else []
    transcript = _format_history_for_prompt(prior)

    return (
        "You are a Slack message interpreter for BugBot.\n"
        "Your job is to interpret the user's message and return STRICT JSON only (no prose, no markdown).\n"
        "Do NOT drop details like 'line 147'—keep the question intact.\n\n"
        "Return EXACTLY one JSON object with these keys:\n"
        '- "ticket_key": string|null (e.g., "LAB-26929")\n'
        '- "should_override_ticket": boolean\n'
        '- "question": string (required; the user\'s actual question)\n'
        '- "pr_url": string|null (GitHub PR URL if provided)\n'
        '- "pr_number": integer|null (PR number if user refers to PR by number)\n'
        '- "github_repo": string|null (e.g., "BioData/Labguru" if inferable)\n\n'
        "Rules:\n"
        "- If the user explicitly indicates a different ticket than the session, set ticket_key and should_override_ticket=true.\n"
        "- If this is a follow-up and the ticket is not specified, set ticket_key=null and should_override_ticket=false.\n"
        "- Only set pr_number/pr_url when the user clearly refers to a PR (e.g., GitHub URL or \"PR 123\").\n"
        "- Prefer github_repo from context; otherwise null.\n\n"
        f"Session state JSON:\n{json.dumps(session_state, ensure_ascii=False)}\n\n"
        f"Conversation so far:\n{transcript}\n\n"
        "Raw Slack message (verbatim):\n"
        f"{raw}\n\n"
        "Slack message with leading @mentions removed (if any):\n"
        f"{cleaned}\n"
    )

def interpret_message_via_llm(*, raw_text: str, session: Session, is_app_mention: bool) -> dict[str, Any]:
    _log_content(logging.getLogger("slackbot"), "BUGBOT_USER_MESSAGE", raw_text or "")
    prompt = _cursor_interpreter_prompt(raw_text=raw_text, session=session, is_app_mention=is_app_mention)
    out = _run_cursor_agent(prompt)
    # Do NOT normalize for Slack here; we need JSON.
    candidate = _extract_first_json_object(out) or ""
    try:
        parsed = json.loads(candidate)
    except Exception:
        raise RuntimeError(f"Interpreter did not return valid JSON. Raw output: {out[:5000]}")
    if not isinstance(parsed, dict):
        raise RuntimeError("Interpreter JSON was not an object.")
    try:
        _log_content(logging.getLogger("slackbot"), "BUGBOT_INTERPRETER_JSON", json.dumps(parsed, ensure_ascii=False))
    except Exception:
        pass
    return parsed


def _normalize_ticket_key(raw: str) -> Optional[str]:
    if not raw:
        return None

    m = re.search(rf"\b{re.escape(PROJECT_PREFIX)}-\d+\b", raw, re.IGNORECASE)
    if m:
        num = m.group(0).split("-", 1)[1]
        return f"{PROJECT_PREFIX}-{num}"

    return None


def _parse_app_mention_text(text: str) -> Optional[ParsedRequest]:
    """
    Slack app_mention text looks like: "<@U12345> LAB-28330 what changed?"
    """
    text = (text or "").strip()
    if not text:
        return None

    # Remove the leading mention token(s)
    text = re.sub(r"^(?:<@[^>]+>\s*)+", "", text).strip()
    if not text:
        return None

    ticket_key = _normalize_ticket_key(text)
    if not ticket_key:
        return None

    question = text
    question = re.sub(rf"\b{re.escape(ticket_key)}\b", "", question, count=1, flags=re.IGNORECASE).strip()
    question = re.sub(rf"\b{re.escape(ticket_key.split('-', 1)[1])}\b", "", question, count=1).strip()

    if not question:
        return ParsedRequest(ticket_key=ticket_key, question="")

    return ParsedRequest(ticket_key=ticket_key, question=question)


def _find_review_context_file(ticket_key: str) -> Optional[str]:
    if not REVIEW_CONTEXT_DIR or not os.path.isdir(REVIEW_CONTEXT_DIR):
        return None

    direct = os.path.join(REVIEW_CONTEXT_DIR, f"{ticket_key}.md")
    if os.path.exists(direct):
        return direct

    ticket_num = ticket_key.split("-", 1)[1]
    candidates: list[str] = []
    for name in os.listdir(REVIEW_CONTEXT_DIR):
        path = os.path.join(REVIEW_CONTEXT_DIR, name)
        if not os.path.isfile(path):
            continue
        low = name.lower()
        if ticket_key.lower() in low or ticket_num in low:
            candidates.append(path)

    if not candidates:
        return None

    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return candidates[0]


def _read_context_md(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return _strip_ansi(f.read()).strip()


def _compact_context_md(full_md: str) -> str:
    s = (full_md or "").strip()

    # Drop huge patch section if present.
    m = re.search(r"^\s*##\s+Patch\b", s, flags=re.IGNORECASE | re.MULTILINE)
    if m:
        s = s[: m.start()].rstrip()

    # Hard-cap prompt size
    max_chars = 12000
    if len(s) > max_chars:
        s = s[:max_chars].rstrip() + "\n\n(TRUNCATED)"

    return s


def _build_prompt(*, ticket_key: str, question: str, context_md: str) -> str:
    return (
        "You are BugBot, a Slack bot that answers questions about a Jira ticket / PR using a saved PR review context file.\n"
        "Your user is chatting with you in Slack and expects Slack-friendly output.\n"
        "Use the provided PR Review Context as the primary source of truth.\n"
        "If needed, you may inspect the local Labguru repo code to verify details.\n"
        "If the context is missing the needed detail, say what is missing.\n"
        "Be concise.\n"
        "Output format for Slack:\n"
        "- Prefer plain text.\n"
        "- Use bullet points prefixed with '• ' (not '-').\n"
        "- Do NOT use Markdown bold like **this** (Slack won't render it). Use plain text or *single-asterisk* if you must.\n"
        "- Avoid headings like '###'.\n\n"
        f"Ticket: {ticket_key}\n"
        f"Question: {question}\n\n"
        "--- BEGIN PR REVIEW CONTEXT ---\n"
        f"{context_md}\n"
        "--- END PR REVIEW CONTEXT ---\n"
    )

def _format_history_for_prompt(history: list[dict[str, str]]) -> str:
    lines: list[str] = []
    for item in history:
        role = item.get("role")
        text = (item.get("text") or "").strip()
        if not text:
            continue
        if role == "user":
            lines.append(f"User: {text}")
        elif role == "assistant":
            lines.append(f"Assistant: {text}")
    return "\n".join(lines).strip()

def _build_prompt_with_history(
    *,
    ticket_key: str,
    question: str,
    context_md: str,
    history: list[dict[str, str]],
) -> str:
    transcript = _format_history_for_prompt(history)
    return (
        "You are BugBot, a Slack bot that answers questions about a Jira ticket / PR using a saved PR review context file.\n"
        "Your user is chatting with you in Slack and expects Slack-friendly output.\n"
        "Use the provided PR Review Context and the conversation so far.\n"
        "You may inspect the local Labguru repo code if needed to answer precisely.\n"
        "If the context is missing the needed detail, say what is missing.\n"
        "Be concise.\n"
        "Output format for Slack:\n"
        "- Prefer plain text.\n"
        "- Use bullet points prefixed with '• ' (not '-').\n"
        "- Do NOT use Markdown bold like **this** (Slack won't render it). Use plain text or *single-asterisk* if you must.\n"
        "- Avoid headings like '###'.\n\n"
        f"Ticket: {ticket_key}\n\n"
        "--- BEGIN PR REVIEW CONTEXT ---\n"
        f"{context_md}\n"
        "--- END PR REVIEW CONTEXT ---\n\n"
        "--- BEGIN CONVERSATION SO FAR ---\n"
        f"{transcript}\n"
        "--- END CONVERSATION SO FAR ---\n\n"
        f"Latest user question: {question}\n"
    )

def _normalize_for_slack(text: str) -> str:
    """
    Slack mrkdwn does not treat **bold** as bold (it uses *bold*).
    Keep formatting simple and safe for Slack rendering.
    """
    s = (text or "").replace("\r\n", "\n").strip()
    if not s:
        return s

    in_code_block = False
    out_lines: list[str] = []
    for line in s.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            out_lines.append(line)
            continue

        if in_code_block:
            out_lines.append(line)
            continue

        # Convert common Markdown bold to Slack-style bold.
        line2 = line.replace("**", "*")

        # Convert Markdown headings to a single plain line.
        m = re.match(r"^\s{0,3}(#{1,6})\s+(.*)$", line2)
        if m:
            title = (m.group(2) or "").strip()
            if title:
                out_lines.append(title)
            continue

        # Convert common Markdown list markers to a single bullet style.
        line2 = re.sub(r"^(\s*)-\s+", r"\1• ", line2)
        line2 = re.sub(r"^(\s*)\*\s+", r"\1• ", line2)

        out_lines.append(line2)

    return "\n".join(out_lines).strip()

def _ensure_labguru_main_worktree(*, log: logging.Logger) -> str:
    """
    Ensure we have a stable working tree representing main branch for general (non-ticket) questions.
    Returns the directory to use as cwd for cursor-agent.
    """
    # If user points it somewhere valid, trust it.
    if LABGURU_MAIN_WORKTREE_DIR and os.path.isdir(LABGURU_MAIN_WORKTREE_DIR):
        return LABGURU_MAIN_WORKTREE_DIR

    if not LABGURU_REPO_DIR or not os.path.isdir(LABGURU_REPO_DIR):
        return BUGBOT_FILES_DIR

    target = LABGURU_MAIN_WORKTREE_DIR
    if not target:
        return LABGURU_REPO_DIR

    os.makedirs(os.path.dirname(target), exist_ok=True)

    # Create/refresh a worktree. This avoids depending on whatever branch the main repo is currently on.
    try:
        subprocess.run(
            ["git", "worktree", "add", "--force", target, LABGURU_MAIN_REF],
            cwd=LABGURU_REPO_DIR,
            check=True,
            capture_output=True,
            text=True,
        )
        log.info("Created/updated Labguru main worktree at %s (%s)", target, LABGURU_MAIN_REF)
        return target
    except Exception as e:
        try:
            log.warning("Could not create Labguru main worktree (%s). Falling back to LABGURU_REPO_DIR. Error: %s", target, e)
        except Exception:
            pass
        return LABGURU_REPO_DIR


def _run_cursor_agent(prompt: str, *, cwd_override: Optional[str] = None) -> str:
    cmd = [CURSOR_BIN, "-p", prompt, "--model", CURSOR_MODEL]
    if cwd_override and os.path.isdir(cwd_override):
        cwd = cwd_override
    else:
        cwd = LABGURU_REPO_DIR if (LABGURU_REPO_DIR and os.path.isdir(LABGURU_REPO_DIR)) else BUGBOT_FILES_DIR

    attempts_total = max(1, int(CURSOR_RETRIES) + 1)
    last_stdout = ""
    last_stderr = ""
    last_rc = 0

    for attempt_idx in range(attempts_total):
        try:
            p = subprocess.run(cmd, text=True, capture_output=True, timeout=CURSOR_TIMEOUT_SECONDS, cwd=cwd)
        except FileNotFoundError:
            return f"Error: `{CURSOR_BIN}` not found on PATH."
        except subprocess.TimeoutExpired:
            return f"Error: `{CURSOR_BIN}` timed out after {CURSOR_TIMEOUT_SECONDS:.0f}s."

        last_stdout = p.stdout or ""
        last_stderr = p.stderr or ""
        last_rc = int(p.returncode)

        combined = _strip_ansi((last_stdout or "") + ("\n" + last_stderr if last_stderr else ""))
        combined_lower = combined.lower()

        # Retry transient cursor-agent failures
        if "connection stalled" in combined_lower and attempt_idx < attempts_total - 1:
            try:
                logging.getLogger("slackbot").warning(
                    "cursor-agent failed with 'Connection stalled' (attempt %d/%d); retrying...",
                    attempt_idx + 1,
                    attempts_total,
                )
            except Exception:
                pass
            time.sleep(max(0.0, CURSOR_RETRY_BACKOFF_SECONDS) * (attempt_idx + 1))
            continue

        # Success or non-retryable failure
        out = combined
        break

    lines: list[str] = []
    for raw in out.splitlines():
        s = raw.strip()
        if not s:
            continue
        if s in DROP_EXACT_LINES:
            continue
        lines.append(s)

    cleaned = "\n".join(lines).strip()

    # Avoid posting raw "Connection stalled" into Slack; make it actionable.
    if "connection stalled" in cleaned.lower():
        return (
            "Error: Cursor connection stalled while generating the answer.\n"
            "Please retry in a moment. If it keeps happening, restart the bot or increase CURSOR_TIMEOUT_SECONDS/CURSOR_RETRIES."
        )

    if not cleaned and last_rc != 0:
        return f"Error: `{CURSOR_BIN}` exited with code {last_rc}."
    return cleaned or "(no response)"


def _chunk_for_slack(text: str, max_chars: int) -> list[str]:
    text = (text or "").strip()
    if not text:
        return ["(empty)"]
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for line in text.splitlines():
        add = line + "\n"
        if buf and (buf_len + len(add) > max_chars):
            chunks.append("".join(buf).rstrip())
            buf = []
            buf_len = 0
        buf.append(add)
        buf_len += len(add)

    if buf:
        chunks.append("".join(buf).rstrip())

    final: list[str] = []
    for c in chunks:
        while len(c) > max_chars:
            final.append(c[:max_chars])
            c = c[max_chars:]
        if c:
            final.append(c)
    return final


def _post_placeholder(*, client, channel: str, thread_ts: str) -> Optional[str]:
    """
    Posts a placeholder message and returns its ts so we can edit it later.
    Falls back to None if posting fails.
    """
    try:
        res = client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="Working on it…")
        ts = res.get("ts") if isinstance(res, dict) else None
        return str(ts) if ts else None
    except Exception:
        return None


def _update_or_post_answer(
    *,
    client,
    channel: str,
    thread_ts: str,
    placeholder_ts: Optional[str],
    answer: str,
) -> None:
    chunks = _chunk_for_slack(answer, SLACK_MAX_CHARS)
    first = chunks[0] if chunks else "(empty)"
    rest = chunks[1:] if len(chunks) > 1 else []

    updated = False
    if placeholder_ts:
        try:
            client.chat_update(channel=channel, ts=placeholder_ts, text=first)
            updated = True
        except Exception:
            updated = False

    if not updated:
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=first)

    for chunk in rest:
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=chunk)

def _schedule_slow_placeholder_update(
    *,
    future,
    client,
    channel: str,
    placeholder_ts: Optional[str],
) -> None:
    """
    If answering takes longer than SLOW_UPDATE_SECONDS, edit the placeholder message once.
    """
    if not placeholder_ts:
        return
    if SLOW_UPDATE_SECONDS <= 0:
        return

    def fire() -> None:
        try:
            if future.done():
                return
            client.chat_update(channel=channel, ts=placeholder_ts, text=SLOW_UPDATE_TEXT)
        except Exception:
            return

    t = threading.Timer(SLOW_UPDATE_SECONDS, fire)
    t.daemon = True
    t.start()


def _handle_message_text(text: str) -> str:
    parsed = _parse_app_mention_text(text)
    if not parsed:
        return f"Usage: `@bugbot {PROJECT_PREFIX}-1234 <question>` (or `@bugbot 1234 <question>`)."

    if not parsed.question:
        return "Please include a question after the ticket key/number."

    path = _find_review_context_file(parsed.ticket_key)
    if not path:
        return (
            f"Could not find a `review_context` summary for `{parsed.ticket_key}`.\n"
            f"Expected a file named like `{parsed.ticket_key}.md` or containing the ticket key under `{REVIEW_CONTEXT_DIR}`."
        )

    context_full = _read_context_md(path)
    context_md = _compact_context_md(context_full)
    prompt = _build_prompt(ticket_key=parsed.ticket_key, question=parsed.question, context_md=context_md)
    return _run_cursor_agent(prompt)

def _handle_dm_text(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return f"Send: `{PROJECT_PREFIX}-1234 <question>` (or `1234 <question>`)."

    ticket_key = _normalize_ticket_key(text)
    if not ticket_key:
        return f"Send: `{PROJECT_PREFIX}-1234 <question>` (or `1234 <question>`)."

    question = text
    question = re.sub(rf"\b{re.escape(ticket_key)}\b", "", question, count=1, flags=re.IGNORECASE).strip()
    question = re.sub(rf"\b{re.escape(ticket_key.split('-', 1)[1])}\b", "", question, count=1).strip()
    if not question:
        return "Please include a question after the ticket key/number."

    path = _find_review_context_file(ticket_key)
    if not path:
        return (
            f"Could not find a `review_context` summary for `{ticket_key}`.\n"
            f"Expected a file named like `{ticket_key}.md` or containing the ticket key under `{REVIEW_CONTEXT_DIR}`."
        )

    context_full = _read_context_md(path)
    context_md = _compact_context_md(context_full)
    prompt = _build_prompt(ticket_key=ticket_key, question=question, context_md=context_md)
    return _run_cursor_agent(prompt)


def _session_key(*, team_id: str, channel_id: str, thread_ts: str) -> str:
    return f"{team_id}:{channel_id}:{thread_ts}"


def _remove_leading_mentions(text: str) -> str:
    return re.sub(r"^(?:<@[^>]+>\s*)+", "", (text or "").strip()).strip()


def _parse_ticket_and_question(*, raw_text: str, is_app_mention: bool) -> tuple[Optional[str], str]:
    # Legacy deterministic parsing kept only for backwards compatibility in older code paths.
    # New behavior routes through interpret_message_via_llm() and does not use this function.
    text = (raw_text or "").strip()
    if is_app_mention:
        text = _remove_leading_mentions(text)
    ticket_key = _normalize_ticket_key(text)
    question = text
    if ticket_key:
        question = re.sub(rf"\b{re.escape(ticket_key)}\b", "", question, count=1, flags=re.IGNORECASE).strip()
    return ticket_key, (question or "").strip()


def _cap_history(history: list[dict[str, str]]) -> list[dict[str, str]]:
    if MAX_TURNS <= 0:
        return []
    if len(history) <= MAX_TURNS:
        return history
    return history[-MAX_TURNS:]


def _answer_with_session(
    *,
    store: SessionStore,
    log: logging.Logger,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    raw_text: str,
    is_app_mention: bool,
) -> str:
    key = _session_key(team_id=team_id, channel_id=channel_id, thread_ts=thread_ts)
    sess = store.get(key)

    interp = interpret_message_via_llm(raw_text=raw_text, session=sess, is_app_mention=is_app_mention)
    ticket_key = interp.get("ticket_key")
    should_override_ticket = bool(interp.get("should_override_ticket"))
    question = str(interp.get("question") or "").strip()
    _log_content(log, "BUGBOT_QUESTION", question)

    pr_url = interp.get("pr_url")
    pr_number = interp.get("pr_number")
    github_repo = interp.get("github_repo")

    if isinstance(ticket_key, str) and ticket_key.strip():
        normalized = _normalize_ticket_key(ticket_key.strip()) or ticket_key.strip().upper()
        if should_override_ticket or sess.ticket_key is None:
            sess.ticket_key = normalized

    if isinstance(github_repo, str) and github_repo.strip():
        sess.github_repo = github_repo.strip()
    if isinstance(pr_url, str) and pr_url.strip():
        sess.pr_url = pr_url.strip()
    if isinstance(pr_number, int):
        sess.pr_number = pr_number
    elif isinstance(pr_number, str) and pr_number.strip().isdigit():
        sess.pr_number = int(pr_number.strip())

    # If no ticket is inferred/known for this thread, answer as a general Labguru main-branch question.
    if not sess.ticket_key:
        if not question:
            return "I couldn't infer the question. Please rephrase what you want to know."

        main_cwd = _ensure_labguru_main_worktree(log=log)
        prior_history = sess.history[:-1] if sess.history else []
        transcript = _format_history_for_prompt(prior_history[-20:])
        prompt = (
            "You are BugBot, a Slack bot that answers questions about the Labguru codebase.\n"
            "Your user is chatting with you in Slack and expects Slack-friendly output.\n"
            f"Answer based on the Labguru repository main branch checkout located at: {main_cwd}\n"
            "Be concise.\n"
            "Output format for Slack:\n"
            "- Prefer plain text.\n"
            "- Use bullet points prefixed with '• ' (not '-').\n"
            "- Avoid headings like '###'.\n\n"
            "--- BEGIN CONVERSATION SO FAR ---\n"
            f"{transcript}\n"
            "--- END CONVERSATION SO FAR ---\n\n"
            f"Latest user question: {question}\n"
        )
        answer = _run_cursor_agent(prompt, cwd_override=main_cwd)
        answer = _normalize_for_slack(answer)
        _log_content(log, "BUGBOT_ANSWER", answer)
        sess.history.append({"role": "assistant", "text": answer})
        sess.history = _cap_history(sess.history)
        sess.updated_at_ms = int(time.time() * 1000)
        store.save()
        return answer

    if not question:
        return "I couldn't infer the question. Please rephrase what you want to know."

    sess.history.append({"role": "user", "text": question})
    sess.history = _cap_history(sess.history)
    sess.updated_at_ms = int(time.time() * 1000)
    store.save()

    path = _find_review_context_file(sess.ticket_key)
    if not path:
        return (
            f"Could not find a `review_context` summary for `{sess.ticket_key}`.\n"
            f"Expected a file named like `{sess.ticket_key}.md` or containing the ticket key under `{REVIEW_CONTEXT_DIR}`."
        )

    context_full = _read_context_md(path)
    context_md = _compact_context_md(context_full)

    # Prompt transcript excludes the just-added user line; the latest question is provided separately.
    prior_history = sess.history[:-1]
    prompt = _build_prompt_with_history(
        ticket_key=sess.ticket_key,
        question=question,
        context_md=context_md,
        history=prior_history,
    )
    answer = _run_cursor_agent(prompt)
    answer = _normalize_for_slack(answer)
    _log_content(log, "BUGBOT_ANSWER", answer)

    sess.history.append({"role": "assistant", "text": answer})
    sess.history = _cap_history(sess.history)
    sess.updated_at_ms = int(time.time() * 1000)
    store.save()

    try:
        log.info("Answered: session=%s ticket=%s history_len=%d", key, sess.ticket_key, len(sess.history))
    except Exception:
        pass

    return answer


def main() -> None:
    log_level = (os.getenv("SLACKBOT_LOG_LEVEL", "INFO") or "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="[slackbot] %(asctime)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("slackbot")
    store = SessionStore(path=SESSIONS_PATH, max_turns=MAX_TURNS)
    # Warm the main worktree path early so general questions are fast.
    _ = _ensure_labguru_main_worktree(log=log)

    slack_bot_token = os.environ.get("SLACK_BOT_TOKEN")
    slack_app_token = os.environ.get("SLACK_APP_TOKEN")  # xapp-... (Socket Mode)

    if not slack_bot_token or not slack_app_token:
        raise SystemExit(
            "Missing env vars. Set:\n"
            "  SLACK_BOT_TOKEN=... (xoxb-...)\n"
            "  SLACK_APP_TOKEN=... (xapp-...)\n"
        )

    app = App(token=slack_bot_token)

    @app.event("app_mention")
    def on_app_mention(body, event, say, context, logger, client):
        try:
            log.info("Received app_mention event: channel=%s user=%s ts=%s text=%r", event.get("channel"), event.get("user"), event.get("ts"), event.get("text"))
        except Exception:
            pass

        # Avoid bot loops
        if event.get("subtype") == "bot_message" or event.get("bot_id"):
            return
        bot_user_id = context.get("bot_user_id")
        if bot_user_id and event.get("user") == bot_user_id:
            return

        text = event.get("text", "") or ""
        channel = event.get("channel")
        thread_ts = event.get("thread_ts") or event.get("ts")
        team_id = body.get("team_id") or event.get("team") or "unknown"

        placeholder_ts = _post_placeholder(client=client, channel=str(channel), thread_ts=str(thread_ts))

        def work():
            session_key = _session_key(team_id=str(team_id), channel_id=str(channel), thread_ts=str(thread_ts))
            _log_banner(log, f"BUGBOT_JOB_START session={session_key}")
            try:
                log.info("Starting answer job: team=%s channel=%s thread_ts=%s", team_id, channel, thread_ts)
                answer = _answer_with_session(
                    store=store,
                    log=log,
                    team_id=str(team_id),
                    channel_id=str(channel),
                    thread_ts=str(thread_ts),
                    raw_text=text,
                    is_app_mention=True,
                )
                _update_or_post_answer(
                    client=client,
                    channel=str(channel),
                    thread_ts=str(thread_ts),
                    placeholder_ts=placeholder_ts,
                    answer=answer,
                )
                _log_banner(log, f"BUGBOT_JOB_END session={session_key} status=ANSWER_SENT")
            except Exception as e:
                logger.exception("slackbot processing failed")
                _update_or_post_answer(
                    client=client,
                    channel=str(channel),
                    thread_ts=str(thread_ts),
                    placeholder_ts=placeholder_ts,
                    answer=f"Error: {e}",
                )
                _log_banner(log, f"BUGBOT_JOB_END session={session_key} status=ERROR_SENT")

        future = _EXECUTOR.submit(work)
        try:
            def _log_future_done(f):
                exc = f.exception()
                if exc:
                    log.error("Answer job crashed: %r", exc)
            future.add_done_callback(_log_future_done)
        except Exception:
            pass
        _schedule_slow_placeholder_update(
            future=future,
            client=client,
            channel=str(channel),
            placeholder_ts=placeholder_ts,
        )

    @app.event("message")
    def on_message(body, event, say, context, logger, client):
        """
        Handle direct messages and group DMs to the bot.
        Slack will send this event only if Event Subscriptions includes message.im / message.mpim
        and the bot has im:history / mpim:history scopes.
        """
        channel_type = event.get("channel_type")
        if channel_type not in ("im", "mpim"):
            return

        # Ignore message edits/joins/etc.
        if event.get("subtype"):
            return

        # Avoid bot loops
        if event.get("bot_id"):
            return
        bot_user_id = context.get("bot_user_id")
        if bot_user_id and event.get("user") == bot_user_id:
            return

        text = event.get("text", "") or ""
        thread_ts = event.get("thread_ts") or event.get("ts")
        channel = event.get("channel")
        team_id = body.get("team_id") or event.get("team") or "unknown"

        try:
            log.info(
                "Received DM message event: channel_type=%s user=%s ts=%s text=%r",
                channel_type,
                event.get("user"),
                event.get("ts"),
                text,
            )
        except Exception:
            pass

        placeholder_ts = _post_placeholder(client=client, channel=str(channel), thread_ts=str(thread_ts))

        def work():
            session_key = _session_key(team_id=str(team_id), channel_id=str(channel), thread_ts=str(thread_ts))
            _log_banner(log, f"BUGBOT_JOB_START session={session_key}")
            try:
                log.info("Starting DM answer job: team=%s channel=%s thread_ts=%s", team_id, channel, thread_ts)
                answer = _answer_with_session(
                    store=store,
                    log=log,
                    team_id=str(team_id),
                    channel_id=str(channel),
                    thread_ts=str(thread_ts),
                    raw_text=text,
                    is_app_mention=False,
                )
                _update_or_post_answer(
                    client=client,
                    channel=str(channel),
                    thread_ts=str(thread_ts),
                    placeholder_ts=placeholder_ts,
                    answer=answer,
                )
                _log_banner(log, f"BUGBOT_JOB_END session={session_key} status=ANSWER_SENT")
            except Exception as e:
                logger.exception("slackbot DM processing failed")
                _update_or_post_answer(
                    client=client,
                    channel=str(channel),
                    thread_ts=str(thread_ts),
                    placeholder_ts=placeholder_ts,
                    answer=f"Error: {e}",
                )
                _log_banner(log, f"BUGBOT_JOB_END session={session_key} status=ERROR_SENT")

        future = _EXECUTOR.submit(work)
        try:
            def _log_future_done(f):
                exc = f.exception()
                if exc:
                    log.error("DM answer job crashed: %r", exc)
            future.add_done_callback(_log_future_done)
        except Exception:
            pass
        _schedule_slow_placeholder_update(
            future=future,
            client=client,
            channel=str(channel),
            placeholder_ts=placeholder_ts,
        )

    log.info("Starting Socket Mode handler…")
    SocketModeHandler(app, slack_app_token).start()


if __name__ == "__main__":
    main()


