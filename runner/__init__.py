"""
Internal modules for BugBot's ticket runner.

This package exists to keep `ticket_runner.py` as a small CLI entrypoint while
isolating domain logic (Jira, git/PR, cursor-agent orchestration, etc.).
"""


