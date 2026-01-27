# Jira → Cursor ticket runner

This folder contains a small CLI script that:

- Runs `karamba new <NUMBER>`
- Fetches Jira ticket details for `LAB-<NUMBER>`
- Runs `cursor-agent` in plan mode and follows `PLAN.md`
  - By default, it runs **headlessly** (no stdin forwarding).
  - Use `--interactive` to allow Cursor to ask clarifying questions in-terminal.
- Adds the Jira label **BugBot** to the ticket (if missing)
- Prints a strict machine-parseable summary block produced by Cursor (commit name + "what did I work on" + impacts + RCA)
- Stages, commits, force-pushes, and opens a PR:
  - `git add -A`
  - `git commit -m "<COMMIT_NAME from Cursor>"`
  - `git push --force --set-upstream origin <current-branch>`
  - `karamba pr LAB-<NUMBER>`

## Requirements

- `karamba` available on your PATH
- `cursor-agent` available on your PATH
- Python 3
- Jira credentials in env vars:
  - `JIRA_EMAIL`
  - `JIRA_API_TOKEN`
  - Optional: `JIRA_BASE_URL` (defaults to `https://labguru.atlassian.net`)

## Usage

```bash
export JIRA_EMAIL="you@company.com"
export JIRA_API_TOKEN="..."

python3 "/Users/jonathaneshel/Desktop/Code/bugbot files/ticket_runner.py" 1234
```

You can also pass `LAB-1234` or a Jira URL containing the ticket number.

Force interactive mode:

```bash
python3 "/Users/jonathaneshel/Desktop/Code/bugbot files/ticket_runner.py" 1234 --interactive
```

## Redo an existing PR

If you already have a PR open and want bugbot to re-run and update that PR:

1) Checkout the PR branch locally in `/Users/jonathaneshel/Desktop/Code/Labguru`
2) Run:

```bash
python3 "/Users/jonathaneshel/Desktop/Code/bugbot files/ticket_runner.py" 1234 --redo-pr
```

This will **skip** `karamba new` and **skip** `karamba pr`, but will still run Cursor, commit, and **force-push** the current branch to update the existing PR.

## Jira autofill from RUNNER_OUTPUT (optional)

If you create a file at:

- `/Users/jonathaneshel/Desktop/Code/bugbot files/JIRA_INSTRUCTIONS`

...the runner will:

- Include its contents as guidance to Cursor when producing the `RUNNER_OUTPUT` block
- Optionally update Jira fields using Jira REST (if you include an enabled JSON config block)

Recommended format:

```text
Plain-English writing instructions for the three Jira fields go here.

[JIRA_FIELDS_JSON]
{
  "enabled": true,
  "updates": [
    { "jira_field_id": "customfield_12345", "source": "WHAT_DID_I_WORK_ON_DEV", "format": "bullets" },
    { "jira_field_id": "customfield_23456", "source": "RCA", "append_source": "RCA_COMMENTS", "format": "rca_with_comments" },
    { "jira_field_id": "customfield_34567", "source": "WHAT_MIGHT_BE_IMPACTED", "format": "bullets" }
  ]
}
[/JIRA_FIELDS_JSON]
```

To disable Jira field updates for a run:

```bash
python3 "/Users/jonathaneshel/Desktop/Code/bugbot files/ticket_runner.py" 1234 --no-jira-field-update
```

## Cursor plan mode

This script always runs `cursor-agent` and enters plan mode by prefixing the prompt with `/plan`.

## Repo directory (where commands run)

By default, `karamba` and `cursor-agent` run inside:

- `/Users/jonathaneshel/Desktop/Code/Labguru`

You can override it:

```bash
python3 "/Users/jonathaneshel/Desktop/Code/bugbot files/ticket_runner.py" 1234 --repo-dir "/path/to/repo"
```

## Real-time logging while Cursor runs

- The script prints lightweight status logs to **stderr** (so it doesn’t interfere with Cursor output).
- If Cursor is quiet for a while, you’ll see a periodic heartbeat like: `cursor-agent is still running (no output yet)...`
- Cursor output is also saved by default to:
  - `/Users/jonathaneshel/Desktop/Code/bugbot files/logs/last_cursor_agent.log`

Override it:

```bash
python3 "/Users/jonathaneshel/Desktop/Code/bugbot files/ticket_runner.py" 1234 --cursor-log-file "/tmp/cursor_LAB-1234.log"
```

Disable writing a log file:

```bash
python3 "/Users/jonathaneshel/Desktop/Code/bugbot files/ticket_runner.py" 1234 --no-cursor-log-file
```

## Non-interactive mode

If you pass `--non-interactive`, the script runs Cursor headlessly (no stdin forwarding). In that case, Cursor cannot
ask you clarifying questions interactively, so it should proceed with best-guess assumptions.

## Cursor timeout

By default the script will time out the Cursor run after 10 minutes:

```bash
python3 "/Users/jonathaneshel/Desktop/Code/bugbot files/ticket_runner.py" 1234 --cursor-timeout-seconds 900
```

## Rules used

- Process: `PLAN.md` (in this folder)
- Extra API guidance: `api summary.md` (in this folder)
- Bugbot teacher rules: `/Users/jonathaneshel/Desktop/Code/DS/app/services/protocol_converter/.cursorrules`

## One-off: write hello world into Jira fields

This script sets **Description** and **What did I work on** for a ticket (default `LAB-30350` example):

```bash
python3 "/Users/jonathaneshel/Desktop/Code/bugbot files/jira_write_hello_world.py" LAB-30350
```

## Orchestrator: create PR then normalize Jira

This runs the full PR flow (via `ticket_runner.py`) and then normalizes Jira fields/workflow:

- Ensures **Refinement status = Refined**
- Ensures **Original estimate = 5m** if empty
- If status is **Pending**: transition via **Ready & Approved** (moves to TODO)
- If status is **TODO/To Do**: transition via **Start Work** (moves to In Progress)

```bash
python3 "/Users/jonathaneshel/Desktop/Code/bugbot files/bugbot_pr_then_jira.py" 30353
```

By default this runs headlessly. If you want an interactive run:

```bash
python3 "/Users/jonathaneshel/Desktop/Code/bugbot files/bugbot_pr_then_jira.py" 30353 --interactive
```

Redo an existing PR (checkout the PR branch locally first):

```bash
python3 "/Users/jonathaneshel/Desktop/Code/bugbot files/bugbot_pr_then_jira.py" 30353 --redo-pr
```

Jira-only normalization (no PR flow):

```bash
python3 "/Users/jonathaneshel/Desktop/Code/bugbot files/jira_update_ticket_state.py" LAB-30353
```

## Jira updater: post-In-Progress autofill + Ready For Review

`jira_update_ticket_state.py` now also does the following once the ticket reaches **In Progress**:

- Fills:
  - `What did I work on` (generated by `cursor-agent`)
  - `What might be impacted` (generated by `cursor-agent`)
  - `Classification` = `Code change`
  - `Service` = `Labguru`
  - `Bug impact` = `Major`
- Adds a Jira comment: `made with BugBot`
- Transitions to **Ready for Review** via action name **`Ready For Review`**

Requires `cursor-agent` on your PATH (or set `CURSOR_BIN`).

## Slackbot (Socket Mode) — answer questions from `review_context/`

This folder also includes `slackbot.py`, a Socket Mode Slack bot that listens for `@mention`s like:

`@bugbot LAB-28330 what changed and what might be impacted?`

Follow-ups:

- In a channel: reply in the **same thread**. After the first message sets the ticket, follow-ups can omit the ticket key.
- In DMs: follow-ups are **thread-only** as well (use Slack “Reply in thread”).

It will:

- Find the matching markdown file in `review_context/`
- Build a bounded prompt (drops the giant `## Patch` section)
- Run `cursor-agent` non-interactively to answer
- Reply in the same Slack thread (chunked if long)

### Slack app configuration

In your Slack app:

- Enable **Socket Mode**
  - Create an **App-Level Token** with scope `connections:write`
  - Save it as `SLACK_APP_TOKEN` (starts with `xapp-`)
- Enable **Event Subscriptions**
  - Subscribe to the bot event: `app_mention`
  - (For DMs) Also subscribe to: `message.im` and `message.mpim`
- OAuth scopes (Bot Token Scopes)
  - `app_mentions:read`
  - `chat:write`
  - (For DMs) Also add: `im:history` and `mpim:history`
- Install the app to your workspace (gives `SLACK_BOT_TOKEN`, starts with `xoxb-`)
- Invite the app to the channel(s) where you want it to respond: `/invite @YourAppName`

### Requirements

- Python 3
- `cursor-agent` on your PATH
- Python dependency:

```bash
python3 -m pip install slack-bolt
```

### Run

```bash
export SLACK_BOT_TOKEN="xoxb-..."
export SLACK_APP_TOKEN="xapp-..."

# Optional:
export CURSOR_MODEL="gpt-5.2-high"
export CURSOR_TIMEOUT_SECONDS="90"

python3 "/Users/jonathaneshel/Desktop/Code/bugbot files/slackbot.py"
```

### Optional environment variables

- `REVIEW_CONTEXT_DIR`: defaults to `/Users/jonathaneshel/Desktop/Code/bugbot files/review_context`
- `LABGURU_REPO_DIR`: defaults to `/Users/jonathaneshel/Desktop/Code/Labguru` (where `cursor-agent` runs so it can read the repo if needed)
- `PROJECT_PREFIX`: defaults to `LAB`
- `CURSOR_BIN`: defaults to `cursor-agent`
- `CURSOR_MODEL`: defaults to `gpt-5.2-high`
- `CURSOR_TIMEOUT_SECONDS`: defaults to `240`
- `SLACK_MAX_CHARS`: defaults to `3500`
- `SLACKBOT_WORKERS`: defaults to `4`
- `SLACKBOT_SESSIONS_PATH`: defaults to `/Users/jonathaneshel/Desktop/Code/bugbot files/.slackbot_sessions.json`
- `SLACKBOT_MAX_TURNS`: defaults to `50` (history entries stored per thread)
- `SLACKBOT_SLOW_UPDATE_SECONDS`: defaults to `30` (after this many seconds, edit the placeholder if still running)
- `SLACKBOT_SLOW_UPDATE_TEXT`: defaults to `Still working…`


