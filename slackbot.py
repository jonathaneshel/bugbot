#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler


BUGBOT_FILES_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_REVIEW_CONTEXT_DIR = os.path.join(BUGBOT_FILES_DIR, "review_context")

REVIEW_CONTEXT_DIR = os.getenv("REVIEW_CONTEXT_DIR", DEFAULT_REVIEW_CONTEXT_DIR)
PROJECT_PREFIX = (os.getenv("PROJECT_PREFIX", "LAB") or "LAB").strip().upper()

CURSOR_BIN = os.getenv("CURSOR_BIN", "cursor-agent")
CURSOR_MODEL = os.getenv("CURSOR_MODEL", "gpt-5")
CURSOR_TIMEOUT_SECONDS = float(os.getenv("CURSOR_TIMEOUT_SECONDS", "90"))

SLACK_MAX_CHARS = int(os.getenv("SLACK_MAX_CHARS", "3500"))
WORKERS = int(os.getenv("SLACKBOT_WORKERS", "4"))

DROP_EXACT_LINES = {"INSTRUCTIONS RECIEVED", "STARTED READING"}

_EXECUTOR = ThreadPoolExecutor(max_workers=WORKERS)


@dataclass(frozen=True)
class ParsedRequest:
    ticket_key: str
    question: str


def _strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", s or "")


def _normalize_ticket_key(raw: str) -> Optional[str]:
    if not raw:
        return None

    m = re.search(rf"\b{re.escape(PROJECT_PREFIX)}-\d+\b", raw, re.IGNORECASE)
    if m:
        num = m.group(0).split("-", 1)[1]
        return f"{PROJECT_PREFIX}-{num}"

    m2 = re.search(r"\b\d+\b", raw)
    if m2:
        return f"{PROJECT_PREFIX}-{m2.group(0)}"

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
        "You are a helpful engineering assistant.\n"
        "Use ONLY the provided PR Review Context to answer.\n"
        "If the context is missing the needed detail, say what is missing.\n"
        "Be concise.\n\n"
        f"Ticket: {ticket_key}\n"
        f"Question: {question}\n\n"
        "--- BEGIN PR REVIEW CONTEXT ---\n"
        f"{context_md}\n"
        "--- END PR REVIEW CONTEXT ---\n"
    )


def _run_cursor_agent(prompt: str) -> str:
    cmd = [CURSOR_BIN, "-p", prompt, "--model", CURSOR_MODEL]
    try:
        p = subprocess.run(cmd, text=True, capture_output=True, timeout=CURSOR_TIMEOUT_SECONDS)
    except FileNotFoundError:
        return f"Error: `{CURSOR_BIN}` not found on PATH."
    except subprocess.TimeoutExpired:
        return f"Error: `{CURSOR_BIN}` timed out after {CURSOR_TIMEOUT_SECONDS:.0f}s."

    out = (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")
    out = _strip_ansi(out)

    lines: list[str] = []
    for raw in out.splitlines():
        s = raw.strip()
        if not s:
            continue
        if s in DROP_EXACT_LINES:
            continue
        lines.append(s)

    cleaned = "\n".join(lines).strip()
    if not cleaned and p.returncode != 0:
        return f"Error: `{CURSOR_BIN}` exited with code {p.returncode}."
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


def main() -> None:
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
    def on_app_mention(body, event, say, context, logger):
        # Avoid bot loops
        if event.get("subtype") == "bot_message" or event.get("bot_id"):
            return
        bot_user_id = context.get("bot_user_id")
        if bot_user_id and event.get("user") == bot_user_id:
            return

        text = event.get("text", "") or ""
        channel = event.get("channel")
        thread_ts = event.get("thread_ts") or event.get("ts")

        try:
            say(text="Working on it…", thread_ts=thread_ts)
        except Exception:
            pass

        def work():
            try:
                answer = _handle_message_text(text)
                for chunk in _chunk_for_slack(answer, SLACK_MAX_CHARS):
                    say(text=chunk, thread_ts=thread_ts)
            except Exception as e:
                logger.exception("slackbot processing failed")
                say(text=f"Error: {e}", thread_ts=thread_ts)

        _EXECUTOR.submit(work)

    SocketModeHandler(app, slack_app_token).start()


if __name__ == "__main__":
    main()


