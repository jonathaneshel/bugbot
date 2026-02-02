from __future__ import annotations

import errno
import os
import select
import subprocess
import sys
import termios
import textwrap
import threading
import time
import tty
from typing import Any, Optional

from .logging import _debug_log, _log, _write_log_line
from .types import RunnerOutput


class ClarificationNeeded(RuntimeError):
    def __init__(self, questions_text: str) -> None:
        super().__init__("Clarification needed")
        self.questions_text = questions_text


def _prepare_cursor_invocation(*, prompt: str, cursor_bin: str) -> tuple[list[str], str]:
    """
    Returns (cmd, effective_prompt) for a single attempt.
    Always uses `/plan` embedded in the prompt (this Cursor CLI does not support `--mode`).
    """
    return [cursor_bin], "/plan\n\n" + prompt


def _wrap_prompt_for_headless_clarifications(prompt: str) -> str:
    """
    In headless mode we cannot answer questions. If clarification is needed, instruct Cursor to
    output ONLY the clarification questions (tech-PM phrasing) and stop.
    """
    wrapper = textwrap.dedent(
        """
        IMPORTANT (headless run):
        - You are running without an interactive human. You cannot ask questions and wait.
        - If you need clarification to proceed confidently, DO NOT attempt the implementation.
        - Instead, output ONLY the clarification questions you need (phrased for a technical PM) in EXACTLY this format:

        CLARIFICATION QUESTIONS:
        1) <question 1>
        2) <question 2>

        - Output nothing else before or after that block.
        - Ask at most 3 questions.
        - If you do NOT need clarification, proceed normally and end with the usual RUNNER_OUTPUT block.
        """
    ).strip()
    return wrapper + "\n\n" + (prompt or "").strip() + "\n"


def _extract_clarification_questions(output: str) -> Optional[str]:
    """
    Returns the clarification questions block if present; else None.
    """
    lines = (output or "").splitlines()
    for i, raw in enumerate(lines):
        line = (raw or "").strip()
        if line == "CLARIFICATION QUESTIONS:":
            # Return from the marker until the end (Cursor was instructed to output only this block).
            return "\n".join(lines[i:]).strip() + "\n"
    return None


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
            last_heartbeat_at = time.time()
            while not stop_event.is_set():
                if heartbeat_seconds and heartbeat_seconds > 0:
                    now = time.time()
                    if now - last_heartbeat_at >= heartbeat_seconds:
                        _log("cursor-agent still running...")
                        last_heartbeat_at = now

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
    Non-interactive capture.
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
            _log(
                f"cursor-agent failed with 'Connection stalled' (attempt {attempt_idx+1}/{attempt_count}); retrying..."
            )
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


def run_cursor_for_runner_output(
    *,
    prompt: str,
    repo_dir: str,
    cursor_log_file: Optional[str],
    interactive: bool,
    heartbeat_seconds: int,
    timeout_seconds: int,
    retries: int,
    cursor_bin: str,
) -> tuple[RunnerOutput, str]:
    """
    Runs cursor-agent (interactive PTY or headless) and returns (RunnerOutput, raw_output_used).
    If runner output is invalid (missing fields / invalid RCA), re-asks once for the full block.
    """
    # Default: headless. Opt-in to interactive.
    if interactive and sys.stdin.isatty():
        _log(f"Running cursor-agent in PTY interactive mode in {repo_dir} (stdin forwarded).")
        output, returncode = _run_cursor_agent_with_pty_capture(
            prompt=prompt,
            repo_dir=repo_dir,
            cursor_log_file=cursor_log_file,
            heartbeat_seconds=heartbeat_seconds,
            cursor_bin=cursor_bin,
        )
        if returncode != 0:
            raise RuntimeError("cursor-agent failed (see output above).")
    else:
        _log(f"Running cursor-agent headlessly in {repo_dir} (no human input).")
        effective_prompt = _wrap_prompt_for_headless_clarifications(prompt)
        output = run_cursor_agent_capture_output(
            prompt=effective_prompt,
            repo_dir=repo_dir,
            cursor_log_file=cursor_log_file,
            timeout_seconds=timeout_seconds,
            retries=retries,
            cursor_bin=cursor_bin,
        )

    questions = _extract_clarification_questions(output)
    if questions is not None:
        raise ClarificationNeeded(questions)

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
            prompt=_wrap_prompt_for_headless_clarifications(followup_prompt),
            repo_dir=repo_dir,
            cursor_log_file=cursor_log_file,
            timeout_seconds=timeout_seconds,
            retries=retries,
            cursor_bin=cursor_bin,
        )

    try:
        runner_output = extract_runner_output_from_text(output)
    except RuntimeError as e:
        msg = str(e)
        if "Missing required runner output fields" in msg:
            output2 = reask_cursor_for_full_block(reason="Runner output missing required fields.")
            output = output2
            runner_output = extract_runner_output_from_text(output2)
        elif "Runner output RCA was not one of the allowed values" in msg:
            output2 = reask_cursor_for_full_block(reason="Runner output RCA invalid.")
            output = output2
            runner_output = extract_runner_output_from_text(output2)
        else:
            raise

    return runner_output, output


