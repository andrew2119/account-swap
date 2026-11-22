# account-swap

`account-swap` moves Claude agents that are already running to another Claude account, each with its exact conversation.
The same conversation id is resumed in the same Herdr pane and the same working folder, now on the target account.
It moves Firstmate workers and leads (second mates) on this machine and keeps each agent's Firstmate record true.

It replaces the job of the archived `/acct2` command, which handed a task to a separate headless Claude on account 2.
It also replaces the by-hand method the main First Mate used (the `acct-move` handoff scripts in the main Firstmate home), which it follows step for step.

It runs only on the captain's word or the main First Mate's routed request; the `account-swap` skill in `skills/account-swap/SKILL.md` tells every agent when it may run the tool.
It never changes which account new workers start on: each Firstmate home's `config/claude-account` pin is the captain's choice, and the tool neither reads nor writes it.

## Accounts

The tool knows exactly three accounts, and `--to` and `--from` accept only the numbers 1, 2 and 3:

| Number | Folder | Notes |
| --- | --- | --- |
| 1 | `~/.claude` | The default login, with `CLAUDE_CONFIG_DIR` unset; Firstmate records it as `account=ordinary`. |
| 2 | `~/.claude-profiles/acct2` | |
| 3 | `~/.claude-profiles/acct3` | Only when that folder exists. |

Every other spelling is a usage error: no alias such as `acct2` or `account 2`, and no folder path.
An agent whose `CLAUDE_CONFIG_DIR` names any other folder, an empty value, or `~/.claude` written out (a login separate from account 1) is refused.

## The three scopes

```sh
account-swap worker <task-id> --to N [--home <main|domain|folder>]
account-swap lead <lead-id> --to N [--with-crew]
account-swap all --from N --to M
```

`worker` takes a task id from any Firstmate home's `state/<task-id>.meta`; `--home` is needed only when two homes record the same id.
`lead` takes a second mate's id from main's records (`kind=secondmate`) and moves that lead's own conversation; `--with-crew` adds the workers recorded in its home.
`all` takes every lead and worker record whose Claude agent runs on account N right now.
Homes are `~/.harness/main/firstmate` and `~/.harness/homes/<domain>/firstmate`.

## The plan first, then an explicit `--go`

Every run prints the plan first.
Without `--go` it stops there, having stopped, copied and written nothing; acting always takes a second run with `--go`.

```text
account-swap plan: every recorded agent on account 2, to account 1 (the default login, CLAUDE_CONFIG_DIR unset)

  agent         home   on now      conversation                           moves to    plan
  lead dom      dom    account 2   0b4e6f2a-6d1c-4f7e-9a51-3c2d8e1f7a90   account 1   move
  worker t1     dom    account 2   5c1a9e37-2b84-4d06-8f3e-7a6b1c0d2e45   account 1   move
  worker t4     dom    account 2   9d27c4b1-8e53-4a9f-b062-1f3e5d7c9a18   account 1   wait: its Firstmate busy record says it is mid-turn; --go waits up to 240 s for its next pause, then leaves it
also on account 2, not moved (no Firstmate worker or lead record): pid 48213 in /Users/example/.harness/main/firstmate
skipped 12 Firstmate records with no Claude agent of theirs running here (finished tasks, other harnesses, other machines)

dry run: nothing was stopped, copied or written. Run again with --go to move the 3 agents marked move or wait.
```

Each row is one of `move`, `wait` (Claude's session record says it is mid-turn or waiting on a prompt, or Firstmate's busy record says busy), `already there`, or `refused` with the reason.
A missing Firstmate busy record supplies no busy signal, while an existing record that is unreadable, non-UTF-8, malformed, lacks `state`, or names a state other than `busy` or `idle` is treated as busy.
A session with no Firstmate record, such as the main First Mate's own, is listed and never moved, and so is the session running the tool.

## What `--go` does for each agent

1. It re-reads the agent and refuses on any change since the plan.
   Before copying, it gets valid workspace and tab values from Herdr, takes the agent's Firstmate record lock, re-reads the record, and keeps a dated backup; any failure refuses the agent while it is still running on the source account.
   A busy agent is re-checked every 10 seconds and moved at its next pause; one still busy after 240 seconds is left running as it was.
   That is the only waiting policy: there is no option to cut a turn short or to change the wait.
2. It copies the conversation into the target account while the agent still runs: `projects/<folder>/<id>.jsonl`, the `<id>/` side folder beside it, and `file-history/<id>/`, each file through a temporary name, and verifies every file by SHA-256.
   An existing target copy is renamed aside as `<name>.older-copy-<time>`, never overwritten.
   If the agent becomes busy before the stop check, a later attempt updates and verifies this run's own copy in place instead of renaming it aside.
3. It registers Firstmate's workspace trust on the target account with the owning home's own `bin/fm-claude-trust.sh` (for a lead, main's helper with `--secondmate-home`), the same step Firstmate takes at launch.
4. Immediately before the stop signal it re-reads the Claude session and Firstmate busy records, requiring the same current conversation and an idle agent as well as the same foreground process and start time.
   An agent that became busy returns to the same wait loop and deadline; only then does the tool send SIGTERM and wait for it to exit.
5. It copies again, refusing if any other process now holds the conversation, and compares the copy with the source in both directions.
   A file this run copied that the source no longer has is removed from the target; any other file in the target that the source does not have fails the comparison and is left in place.
6. It types a resume command into the same pane: the agent's own launch flags with the first prompt dropped, `--resume <id>`, and a short note naming the move, started from a cleared environment (see "Environment and credentials").
7. It proves the move: a new Claude process in the pane whose environment names the target account, the old process gone, and the transcript under the target folder, printed as
   `moved: pid A on account X -> pid B on account Y in pane P; conversation <id> kept; transcript <path>`.
8. Only then it takes the record lock again and updates that agent's Firstmate record: `account=` is set to the target's recorded value (`ordinary` or the profile folder) and `herdr_workspace_id` and `herdr_tab_id` are set to the values checked before the move.
   Nothing else in the record changes, and the dated backup remains as `<id>.meta.pre-account-swap-<time>`.
   Because the record names the account, a later Firstmate relaunch of that agent stays on the target account.
   If the final write fails, the run exits non-zero, says that the agent is running on the target while its record still names the source, and prints the exact account and endpoint values the record needs; it never reports the move as successful.

The outcomes are:

- `moved`: the proof above.
- `refused`: a check failed before the stop signal was sent; the agent is still running as it was.
- `restored`: something failed after the stop signal (the agent outlived the stop wait and exited later, the final comparison failed, or the resume on the target failed), so the agent was resumed on its own account with the same conversation, and its record is unchanged.
- `failed`: the agent was sent the stop signal and either could not be resumed automatically or resumed on the target but its record could not be updated; the message says plainly what state it is in and gives the exact recovery command or record values needed.

Once the stop signal is sent, no outcome ever says the agent is still running as it was.
Nothing on the source account is ever deleted or changed.
Each `--go` run keeps its log and the resume commands for both directions in a private folder, `~/.local/state/account-swap/<time>-<pid>/`, and only one `--go` run acts at a time: a second one started meanwhile stops at once with a message and exit 1.

| Exit | Meaning |
| --- | --- |
| 0 | Every agent in the plan is ready; with `--go`, every one moved or was already there. |
| 1 | At least one agent was refused, left busy, restored or failed, or the process list could not be read. |
| 2 | Usage error, including an account folder that does not exist on this machine. |
| 3 | Nothing matched: no such worker or lead, nothing recorded runs on that account, or Herdr is not available. |

## Environment and credentials

The resume command starts the agent with `/usr/bin/env -i` and only a fixed list of non-secret variables, each with the value the agent was started with: the session basics (`HOME`, `PATH`, `TERM`, the locale and temporary folders and the like), the Herdr, tmux, cmux and Zellij pane identities, Firstmate's task markers and launch settings (`FM_TASK_ID`, `FM_HOME` and the other `FM_*` names Firstmate sets), and Claude's model settings.
`CLAUDE_CONFIG_DIR` is set for the target account, or left unset for account 1.
Firstmate's commit-hook setting (`GIT_CONFIG_COUNT` with `core.hooksPath` pairs) is carried too; an agent with any other git setting in its environment is refused, because such a setting can carry a credential.
Every other variable is left out, so no token or other environment value is ever written into a resume command or a log.
The list is `CARRIED` at the top of `bin/account-swap`.

An agent whose environment holds a variable that can override the chosen account is refused before anything is copied: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_BASE_URL` and the other `ANTHROPIC_*_API_KEY`, `*_AUTH_TOKEN` and `*_BASE_URL` names, `ANTHROPIC_CUSTOM_HEADERS`, `ANTHROPIC_PROFILE`, the `CLAUDE_CODE_OAUTH_*` names, the `CLAUDE_CODE_USE_*` provider switches (Bedrock, Vertex, Foundry and others), and `AWS_BEARER_TOKEN_BEDROCK` (the pattern is `ACCOUNT_OVERRIDE` in `bin/account-swap`).
An agent launched with `--settings` naming a file is refused without that file being opened, because its contents cannot be vouched for.
An inline `--settings` value is refused if it sets environment variables or credential selectors (`apiKeyHelper`, `awsAuthRefresh`, or `awsCredentialExport`), since its launch flags are written into the resume command.
An inline JSON `--mcp-config` value is refused because its environment, headers, arguments or URL can carry credentials into the persisted resume command; a path-valued `--mcp-config` is carried unchanged without opening the file.
The refusal names only the variable, key or flag; the tool never prints, copies, unsets or edits its value.

## Safety rules

- It never touches credentials, the Keychain, login items, any account's `settings.json`, or any home's `config/claude-account` pin.
- It never opens, copies or names a protected conversation: one whose id starts with a prefix listed one per line in `~/.config/account-swap/protected-conversations`; the plan and the log show only their first eight characters.
  That file is local to the machine and never part of a repository; it lists, for example, the conversations main's own move scripts never touch, and without it no conversation is protected.
  If that file exists but cannot be read, is not UTF-8, or contains an invalid non-comment line, the run refuses before printing a plan.
- It never moves a conversation that another process still holds, and never moves the session that runs it.
- It moves only agents with a Firstmate record on this machine, in a Herdr pane; remote agents and other harnesses are refused.
- It refuses, before stopping anything, an agent whose pane runs no Claude, more than one Claude, or a Claude that is not the foreground program or was not started from the pane's shell; whose Firstmate endpoint changed after the plan; whose session record is missing, lacks a matching process start time, is stale, or names no valid conversation; whose transcript is missing or ambiguous; or whose launch cannot be carried onto a resume (`--continue`, a resume of a different conversation, more than one prompt, or a flag the tool does not know).
- It uses Herdr's `pane process-info`, `pane get` and `pane run` only; it never starts, stops or rearranges a Herdr pane or session.
- It changes no Firstmate code: it calls the home's own trust and lock helpers as they are.

## Installing

The dotfiles repository installs `bin/account-swap` as `~/.local/bin/account-swap` and links the skill, at a pinned commit of this repository; a new version reaches the machine only when dotfiles moves that pin.
The tool is one Python file with no dependencies beyond the standard library.
It needs `python3`, `bash`, `ps` and `herdr` on `PATH`, and reads process environments through the macOS kernel interface (`/proc` on Linux).

## Tests

```sh
python3 tests/account_swap_test.py -v
```

The suite runs everything in a throwaway home with stand-ins: each Herdr pane is a real `/bin/sh` reading typed commands from a FIFO, and `herdr`, `claude` and each Firstmate home's trust and lock helpers are stubs that log every call.
No real agent, Claude account folder, Firstmate home, credential or Herdr session is touched.
GitHub Actions runs it on Linux (`ubuntu-latest`) on every pull request and every push to `main` (`.github/workflows/test.yml`).
