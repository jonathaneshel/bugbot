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
import datetime
import json
import os
import re
import subprocess
import sys
import textwrap
import time
from typing import Any, Optional


from runner.constants import (
    API_RULES_MD_PATH,
    BUGBOT_TEACHER_CURSORRULES_PATH,
    DEFAULT_CURSOR_LOG_FILE,
    DEFAULT_JIRA_BASE_URL,
    DEFAULT_PROJECT_PREFIX,
    DEFAULT_REPO_DIR,
    DEFAULT_RUNNER_OUTPUT_JSON_FILE,
    JIRA_INSTRUCTIONS_PATH,
    PLAN_MD_PATH,
)
from runner.logging import _debug_log, _log, _write_log_line
from runner.jira import (
    _format_for_jira,
    _parse_jira_instructions,
    _runner_output_value,
    ensure_bugbot_label,
    fetch_jira_issue,
    update_jira_issue_fields,
)
from runner.git_ops import (
    _ensure_on_ticket_branch,
    _git_current_branch,
    _git_untracked_files,
    _run_cmd,
    run_git_commit_push_and_pr,
    run_karamba_new_in_repo,
)
from runner.cursor_agent import run_cursor_for_runner_output
from runner.review_context import write_pr_review_context_file
from runner.types import JiraIssue, PrCreationError, RunnerOutput
from runner import cursor_agent as _cursor_agent


def _write_runner_output_json(*, path: str, issue_key: str, runner_output: RunnerOutput) -> None:
    """
    Persist the parsed RunnerOutput so other scripts (e.g. Jira updater) can reuse it
    without calling cursor-agent again.
    """
    log_path = os.path.abspath(os.path.expanduser(path))
    parent = os.path.dirname(log_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    payload = {
        "issue_key": issue_key,
        # Mirror the machine-parseable keys used in RUNNER_OUTPUT for stability.
        "COMMIT_NAME": runner_output.commit_name,
        "WHAT_DID_I_WORK_ON_DEV": runner_output.what_did_i_work_on_dev,
        "WHAT_DID_I_WORK_ON_TECH_PM": runner_output.what_did_i_work_on_tech_pm,
        "WHAT_DID_I_WORK_ON_NON_TECH_PM": runner_output.what_did_i_work_on_non_tech_pm,
        "WHAT_MIGHT_BE_IMPACTED": runner_output.what_might_be_impacted,
        "RCA": runner_output.rca,
        "RCA_COMMENTS": runner_output.rca_comments,
        "SPECS_STATUS": runner_output.specs_status,
        "SPECS_DETAILS": runner_output.specs_details,
    }
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
    _log(f"Wrote runner output JSON: {log_path}")


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def _read_text_if_exists(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


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


def run_karamba_new(ticket_number: str) -> None:
    run_karamba_new_in_repo(
        issue_key=f"{DEFAULT_PROJECT_PREFIX}-{ticket_number}",
        repo_dir=DEFAULT_REPO_DIR,
    )

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
    return _cursor_agent._prepare_cursor_invocation(prompt=prompt, cursor_bin=cursor_bin)


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
    return _cursor_agent._run_cursor_agent_with_pty_capture(
        prompt=prompt,
        repo_dir=repo_dir,
        cursor_log_file=cursor_log_file,
        heartbeat_seconds=heartbeat_seconds,
        cursor_bin=cursor_bin,
    )
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
    return _cursor_agent.extract_commit_name_from_text(output)
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
    return _cursor_agent.extract_runner_output_from_text(output)
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
    return _cursor_agent.run_cursor_agent_capture_output(
        prompt=prompt,
        repo_dir=repo_dir,
        cursor_log_file=cursor_log_file,
        timeout_seconds=timeout_seconds,
        retries=retries,
        cursor_bin=cursor_bin,
    )
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
        "--runner-output-json",
        default=DEFAULT_RUNNER_OUTPUT_JSON_FILE,
        help=f"Write parsed RUNNER_OUTPUT fields to this JSON file (default: {DEFAULT_RUNNER_OUTPUT_JSON_FILE}).",
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
        "Will auto-checkout the ticket branch (by searching local/remote branches for the issue key) "
        "and then run Cursor, commit, and force-push.",
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
        _log("redo-pr mode enabled: ensuring we are on the correct ticket branch...")
        _ensure_on_ticket_branch(repo_dir=repo_dir, issue_key=issue_key)

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
    # Best-effort; failures are warnings only.
    ensure_bugbot_label(
        jira_base_url=args.jira_base_url,
        issue_key=issue_key,
        email=jira_email,
        api_token=jira_token,
    )

    prompt = build_cursor_prompt(issue)
    runner_output, _ = run_cursor_for_runner_output(
        prompt=prompt,
        repo_dir=repo_dir,
        cursor_log_file=cursor_log_file,
        interactive=bool(args.interactive),
        heartbeat_seconds=args.heartbeat_seconds,
        timeout_seconds=args.cursor_timeout_seconds,
        retries=args.cursor_retries,
        cursor_bin=args.cursor_bin,
    )

    try:
        if str(args.runner_output_json or "").strip():
            _write_runner_output_json(
                path=args.runner_output_json,
                issue_key=issue_key,
                runner_output=runner_output,
            )
    except Exception as e:
        _log(f"WARNING: failed to write runner output JSON: {e}")

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


