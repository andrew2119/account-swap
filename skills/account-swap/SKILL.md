---
name: account-swap
description: Use only when the captain says, or the main First Mate's routed request asks, to move a running Claude worker, a lead (second mate), or every agent on one Claude account to another account. Runs the account-swap tool, which prints the plan first and moves each agent with its exact conversation. Replaces the archived /acct2 command's job of handing work to another account.
---

# Moving running agents to another Claude account

`account-swap` (installed at `~/.local/bin/account-swap` by the dotfiles repository, at a pinned commit of the account-swap repository) moves running Claude agents from one Claude account to another.
Each agent keeps its exact conversation: the same conversation id, resumed in the same Herdr pane and the same folder, on the target account.

The accounts are numbered, and only these three numbers are accepted:

- Account 1 is the default login (`~/.claude`, CLAUDE_CONFIG_DIR unset; Firstmate records it as `account=ordinary`).
- Account 2 is `~/.claude-profiles/acct2`.
- Account 3 is `~/.claude-profiles/acct3`, when that folder exists.

No alias (`acct2`, `account 2`) and no other profile folder is an account.

## When to use it

Only on the captain's word, or on the main First Mate's routed request that relays it.
Never on your own initiative: not because an account looks busy or close to its limit, and not to tidy up.
`claude-account-room` tells you which account has room; whether anything moves stays the captain's decision.
A lead that thinks a move would help routes the question up to main instead of running the tool.

It never changes which account new workers start on.
Each Firstmate home's `config/claude-account` pin is the captain's choice alone; the tool never touches it, and you never edit it either.

## Always show the plan first

Run the tool without `--go` first.
That run only reads: nothing is stopped, copied or written.

```sh
account-swap worker <task-id> --to 1          # one worker (add --home <domain> if two homes record that id)
account-swap lead <lead-id> --to 2            # one lead's own conversation (add --with-crew for its workers)
account-swap all --from 2 --to 1              # every recorded worker and lead running on account 2
```

The plan lists each agent, its Firstmate home, the account it runs on now, its conversation id, the account it moves to, and what will happen: `move`, `wait` (it is mid-turn), `already there`, or `refused` with the reason.
For `all` it also lists Claude sessions on that account that no Firstmate record owns, such as the main First Mate's own session; those are never moved.
Relay the plan in plain words when the request came through main, and act only when the request covers exactly what the plan shows.

## Acting

Run the same command again with `--go`.
It can take several minutes: a busy agent is moved at its next pause, and one still busy after 240 seconds is left running as it was.
The tool rechecks the conversation and both busy records immediately before stopping; an agent that became busy returns to the same wait and deadline.
There is no option to cut a turn short or to change that wait.
Give the command a long timeout or run it in the background; never interrupt it mid-move.

For each agent the tool first validates and backs up its Firstmate record under the record lock, then copies the conversation (transcript, side folder, file history) to the target account and verifies it byte for byte, registers Firstmate's workspace trust there, stops the agent, copies and verifies again in both directions, resumes the same conversation in the same pane, and proves the move before it updates that record (`account=` and the pane's workspace and tab) under the lock.
If the final record write fails, it reports the agent on the target with the exact record values needed and exits non-zero rather than reporting success.
Each resumed agent receives a short note saying it was moved.
Nothing on the source account is deleted, and an existing copy on the target is set aside under a dated name.
The resume commands and a log of every `--go` run are kept under `~/.local/state/account-swap/`.

The resumed agent starts with only a fixed list of non-secret environment variables carried over (the session basics, the Herdr pane identity, Firstmate's task markers and commit-hook setting, and Claude's model settings), plus the target account.
Every other variable is left out, so no token or other environment value is ever written into a resume command or a log.
An agent whose environment holds a variable that can override the chosen account (such as an API key, an auth token, a base URL, or a Bedrock, Vertex or Foundry switch) is refused.
A `--settings` file is refused without being opened, and inline settings that select a credential or set environment variables are refused.
An inline JSON `--mcp-config` is refused because it can carry credentials, while a path-valued one is carried without opening the file.
The message names only the variable, key or flag, and its value is never printed, copied or changed.

## Reading the result

- `moved: pid A on account X -> pid B on account Y in pane P; conversation <id> kept; transcript <path>` is the proof: a new process on the target account, the same conversation id, and the transcript under the target folder.
- `refused`: a check failed before the stop signal; the agent is still running exactly as it was.
- `restored`: something failed after the stop signal, so the agent was resumed on its own account with the same conversation; its record is unchanged.
- `failed`: the agent was sent the stop signal and could not be resumed automatically; the message says what state it is in and the exact command to type into its pane to resume it; report it to main or the captain rather than improvising.

Exit 0 means every agent in the plan is ready or moved; 1 means at least one was refused, left busy or not moved; 2 is a usage error; 3 means nothing matched.

## What it never does

It never touches credentials, the Keychain, login items, any account's settings, or any home's `config/claude-account` pin.
It never opens, copies or names a protected conversation: one whose id starts with a prefix listed in this machine's `~/.config/account-swap/protected-conversations`, one per line (main's own protected conversations are listed there, never in a repository); an unreadable, malformed, or non-UTF-8 list refuses the run before the plan.
It never moves a conversation that another process still holds, an agent whose Firstmate endpoint changed or whose session record does not match the live process start, or the session that runs it.
It moves only agents with a Firstmate record on this machine, in a Herdr pane; remote agents and other harnesses are refused.

## The archived /acct2 command

The archived `/acct2` command handed a task to a separate headless Claude on account 2.
Its job, putting work on another account, is now done by moving the agent that already holds the work, with its whole conversation, through this tool; for new workers the captain chooses the home's account pin.
Do not recreate `/acct2` or start a second headless Claude to hand work across.
