from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class JiraIssue:
    key: str
    url: str
    summary: str
    issue_type: str
    priority: str
    description_text: str


@dataclass(frozen=True)
class RunnerOutput:
    commit_name: str
    what_did_i_work_on_dev: str
    what_did_i_work_on_tech_pm: str
    what_did_i_work_on_non_tech_pm: str
    what_might_be_impacted: str
    rca: str
    # Optional; not required in RUNNER_OUTPUT anymore.
    rca_comments: str
    specs_status: str
    specs_details: str


class PrCreationError(RuntimeError):
    def __init__(self, message: str, *, output: str) -> None:
        super().__init__(message)
        self.output = output


