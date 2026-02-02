from __future__ import annotations

import os
import time

# `constants.py` lives under `bugbot files/runner/`, so go one directory up to find the
# "bugbot files" root dir.
BUGBOT_FILES_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_JIRA_BASE_URL = "https://labguru.atlassian.net"
DEFAULT_PROJECT_PREFIX = "LAB"
DEFAULT_REPO_DIR = "/Users/jonathaneshel/Desktop/Code/Labguru"

DEFAULT_CURSOR_LOG_FILE = os.path.join(BUGBOT_FILES_DIR, "logs", "last_cursor_agent.log")
DEFAULT_RUNNER_OUTPUT_JSON_FILE = os.path.join(BUGBOT_FILES_DIR, "logs", "last_runner_output.json")

DEBUG_NDJSON_LOG_PATH = os.path.join(BUGBOT_FILES_DIR, ".cursor", "debug.log")
DEBUG_RUN_ID = f"run-{int(time.time())}"

BUGBOT_JIRA_LABEL = "BugBot"

PLAN_MD_PATH = os.path.join(BUGBOT_FILES_DIR, "PLAN.md")
API_RULES_MD_PATH = os.path.join(BUGBOT_FILES_DIR, "api summary.md")
BUGBOT_TEACHER_CURSORRULES_PATH = (
    "/Users/jonathaneshel/Desktop/Code/DS/app/services/protocol_converter/.cursorrules"
)
JIRA_INSTRUCTIONS_PATH = os.path.join(BUGBOT_FILES_DIR, "JIRA_INSTRUCTIONS")
_JIRA_FIELDS_JSON_BEGIN = "[JIRA_FIELDS_JSON]"
_JIRA_FIELDS_JSON_END = "[/JIRA_FIELDS_JSON]"

REVIEW_CONTEXT_DIR = os.path.join(BUGBOT_FILES_DIR, "review_context")
MAX_REVIEW_CONTEXT_DESCRIPTION_CHARS = 8000
MAX_REVIEW_CONTEXT_DIFF_CHARS = 12000


