#!/usr/bin/env python3
"""
Orchestrator:
1) Run bugbot PR flow (ticket_runner.py) for a given ticket number
2) Normalize Jira state afterwards (refinement/estimate/transitions)

Usage:
  python3 bugbot_pr_then_jira.py 30353
  python3 bugbot_pr_then_jira.py LAB-30353
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from typing import Optional


BUGBOT_FILES_DIR = os.path.dirname(os.path.abspath(__file__))
TICKET_RUNNER = os.path.join(BUGBOT_FILES_DIR, "ticket_runner.py")
JIRA_UPDATER = os.path.join(BUGBOT_FILES_DIR, "jira_update_ticket_state.py")
DEFAULT_RUNNER_OUTPUT_JSON = os.path.join(BUGBOT_FILES_DIR, "logs", "last_runner_output.json")


def _extract_ticket_number(raw: str) -> str:
    m = re.search(r"\b(\d+)\b", raw or "")
    if not m:
        raise ValueError(f"Could not find a ticket number in: {raw!r}")
    return m.group(1)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Create PR from ticket, then normalize Jira state.")
    parser.add_argument("ticket", help="Ticket number or LAB-<NUMBER>")
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Run ticket_runner in interactive mode (default is headless).",
    )
    parser.add_argument(
        "--redo-pr",
        action="store_true",
        help="Redo an existing PR: forwards --redo-pr to ticket_runner (skips karamba new/pr; commits + force-pushes current branch).",
    )
    parser.add_argument(
        "--runner-output-json",
        default=DEFAULT_RUNNER_OUTPUT_JSON,
        help=f"Path for runner output JSON handoff between ticket_runner and jira_update_ticket_state (default: {DEFAULT_RUNNER_OUTPUT_JSON}).",
    )
    args = parser.parse_args(argv)

    ticket_number = _extract_ticket_number(args.ticket)
    issue_key = f"LAB-{ticket_number}"

    if not os.path.exists(TICKET_RUNNER):
        print(f"Missing ticket runner: {TICKET_RUNNER}", file=sys.stderr)
        return 2
    if not os.path.exists(JIRA_UPDATER):
        print(f"Missing jira updater: {JIRA_UPDATER}", file=sys.stderr)
        return 2

    print(f"[bugbot_pr_then_jira] Running PR flow via ticket_runner.py for {issue_key}...")
    runner_cmd = [sys.executable, TICKET_RUNNER, ticket_number]
    if not args.interactive:
        runner_cmd.append("--non-interactive")
    if args.redo_pr:
        runner_cmd.append("--redo-pr")
    if str(args.runner_output_json or "").strip():
        runner_cmd.extend(["--runner-output-json", args.runner_output_json])
    subprocess.run(runner_cmd, check=True)

    print(f"[bugbot_pr_then_jira] Normalizing Jira state for {issue_key}...")
    jira_cmd = [sys.executable, JIRA_UPDATER, issue_key]
    if str(args.runner_output_json or "").strip():
        jira_cmd.extend(["--runner-output-json", args.runner_output_json])
    subprocess.run(jira_cmd, check=True)

    print("[bugbot_pr_then_jira] Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


