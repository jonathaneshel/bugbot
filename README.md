# Jira → Cursor ticket runner

This folder contains a small CLI script that:

- Runs `karamba new <NUMBER>`
- Fetches Jira ticket details for `LAB-<NUMBER>`
- Runs `cursor-agent` in plan mode and follows `PLAN.md`
  - By default, it will run **interactive** (stdin forwarded) so Cursor can ask up to **2** clarifying questions if needed.
  - Use `--non-interactive` to run headlessly (no stdin forwarding).
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

Force headless mode:

```bash
python3 "/Users/jonathaneshel/Desktop/Code/bugbot files/ticket_runner.py" 1234 --non-interactive
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


