#!/usr/bin/env python3
"""Behaviour tests for bin/account-swap (see README.md).

Everything runs in a throwaway HOME with stand-ins: each Herdr pane is a real
/bin/sh reading typed commands from a FIFO, `herdr` is a stub that reports and
types into those panes, `claude` is a stub that keeps a transcript and a
session record the way Claude does, and each Firstmate home has stub trust and
record-lock helpers that log every call. No real agent, Claude account folder,
Firstmate home, credential or Herdr session is read for writing or signalled:
the tool only ever sees the stand-ins' process ids.

Covered: the plan output and that a plan run changes nothing, the refusals
(protected and still-open conversations, unknown accounts, launch shapes a
resume cannot keep, account overrides in the environment, a busy agent), a
successful worker move and lead move that keep the same conversation id, the
Firstmate record update with its dated copy, an existing target copy set
aside, that no environment value outside the carried names is ever written,
every failure after the stop signal (a resume that fails on the target, an
agent that outlives the stop wait or never exits, a target that gains a file
the source does not have), the final copy compared both ways, usage errors,
and the skill's frontmatter.
"""

from __future__ import annotations

import errno
import importlib.machinery
import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "bin/account-swap"
SKILL = REPO / "skills/account-swap/SKILL.md"
SESSION = "test-session"

# Runs the tool with some of its timing constants replaced, so the slow paths
# (a busy agent, a stop that times out) take seconds instead of minutes.
CONSTANTS_RUNNER = r'''
import importlib.machinery, importlib.util, json, sys
tool, constants, arguments = sys.argv[1], json.loads(sys.argv[2]), sys.argv[3:]
sys.argv = [tool, *arguments]
loader = importlib.machinery.SourceFileLoader("account_swap", tool)
module = importlib.util.module_from_spec(importlib.util.spec_from_loader("account_swap", loader))
sys.modules["account_swap"] = module
loader.exec_module(module)
for name, value in constants.items():
    if not isinstance(getattr(module, name, None), int):
        raise SystemExit(f"the tool has no timing constant {name}")
    setattr(module, name, value)
sys.exit(module.main(arguments))
'''

STUB_CLAUDE = r'''#!{python}
import json, os, re, signal, subprocess, sys, time, uuid
from pathlib import Path

# The resumed process starts from the tool's cleared environment, so the stub
# finds the sandbox from where it lives rather than from a variable.
state = Path(__file__).resolve().parent.parent / "state"
if "CLAUDE_CONFIG_DIR" in os.environ:
    config = Path(os.environ["CLAUDE_CONFIG_DIR"])
else:
    config = Path(os.environ["HOME"]) / ".claude"
arguments = sys.argv[1:]
pid = os.getpid()
with (state / "claude.log").open("a") as log:
    log.write(json.dumps({"pid": pid, "argv": arguments, "env": dict(os.environ), "config": str(config)}) + "\n")
if "--resume" in arguments:
    sid = arguments[arguments.index("--resume") + 1]
    found = sorted(config.glob(f"projects/*/{sid}.jsonl"))
    failing = state / "fail-resume-in"
    if not found or (failing.is_file() and failing.read_text().strip() == str(config)):
        print(f"No conversation found with session ID: {sid}", file=sys.stderr)
        sys.exit(1)
    transcript = found[0]
else:
    sid = os.environ.get("STUB_SESSION_ID") or str(uuid.uuid4())
    transcript = config / "projects" / re.sub(r"[^A-Za-z0-9]", "-", os.getcwd()) / f"{sid}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)


def note(kind, **extra):
    with transcript.open("a") as handle:
        handle.write(json.dumps({"type": kind, "pid": pid, **extra}) + "\n")


def publish(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data))
    os.replace(temporary, path)


note("start")
started = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                         env={**os.environ, "LC_ALL": "C", "TZ": "UTC"}).stdout.strip()
session = config / "sessions" / f"{pid}.json"
process = state / "procs" / f"{pid}.json"


def finish(*_):
    # Stand-ins for what a real agent can do while it shuts down.
    time.sleep(float(os.environ.get("STUB_TERM_DELAY", "0")))
    if os.environ.get("STUB_DELETE_ON_TERM"):
        dropped = transcript.parent / sid / os.environ["STUB_DELETE_ON_TERM"]
        dropped.unlink()
        try:
            dropped.parent.rmdir()
        except OSError:
            pass
    if os.environ.get("STUB_WRITE_ON_TERM"):
        foreign = Path(os.environ["STUB_WRITE_ON_TERM"]) / "projects" / transcript.parent.name / sid / "foreign.txt"
        foreign.parent.mkdir(parents=True, exist_ok=True)
        foreign.write_text("written into the target by another process\n")
    note("end")
    for path in (session, process):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    sys.exit(0)


signal.signal(signal.SIGTERM, signal.SIG_IGN if os.environ.get("STUB_IGNORE_TERM") else finish)
signal.signal(signal.SIGHUP, finish)
publish(process, {"argv": ["claude", *arguments], "cwd": os.getcwd()})
publish(session, {"pid": pid, "sessionId": sid, "cwd": os.getcwd(), "kind": "interactive",
                  "status": os.environ.get("STUB_STATUS", "idle"), "procStart": started})
while True:
    time.sleep(1)
'''

STUB_HERDR = r'''#!{python}
import json, os, subprocess, sys
from pathlib import Path

state = Path(os.environ["STUB_STATE"])
arguments = sys.argv[1:]
with (state / "herdr.log").open("a") as log:
    log.write(json.dumps(arguments) + "\n")
session = None
if "--session" in arguments:
    index = arguments.index("--session")
    session = arguments[index + 1]
    del arguments[index:index + 2]


def fail(text):
    print(text, file=sys.stderr)
    sys.exit(1)


if arguments[:2] == ["pane", "process-info"] and "--pane" in arguments:
    pane = arguments[arguments.index("--pane") + 1]
elif arguments[:2] in (["pane", "get"], ["pane", "run"]) and len(arguments) >= 3:
    pane = arguments[2]
else:
    fail("stub herdr: unsupported command")
path = state / "panes" / (pane.replace(":", "_") + ".json")
if session != "{session}" or not path.is_file():
    fail(f"pane {pane} not found")
info = json.loads(path.read_text())
if arguments[1] == "run":
    descriptor = os.open(info["fifo"], os.O_WRONLY | os.O_NONBLOCK)
    os.write(descriptor, (" ".join(arguments[3:]) + "\n").encode())
    os.close(descriptor)
    print(json.dumps({"id": "1", "result": {}}))
    sys.exit(0)
if arguments[1] == "get":
    print(json.dumps({"id": "1", "result": {"pane": {
        "pane_id": pane, "workspace_id": info.get("workspace"), "tab_id": info.get("tab"),
        "agent": "claude", "cwd": info["cwd"]}}}))
    sys.exit(0)
shell = info["shell"]
rows = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,stat=,comm="], capture_output=True, text=True).stdout
children = []
for row in rows.splitlines():
    parts = row.split(None, 3)
    if len(parts) >= 3 and parts[1] == str(shell) and not parts[2].startswith("Z"):
        children.append((int(parts[0]), parts[3] if len(parts) > 3 else ""))


def entry(pid, command):
    record = state / "procs" / f"{pid}.json"
    if record.is_file():
        data = json.loads(record.read_text())
        return {"pid": pid, "argv0": "claude", "argv": data["argv"], "name": "2.1.330", "cwd": data["cwd"]}
    name = os.path.basename(command)
    return {"pid": pid, "argv0": name, "argv": [name], "name": name, "cwd": info["cwd"]}


if children:
    foreground, processes = children[0][0], [entry(pid, command) for pid, command in children]
else:
    foreground = shell
    processes = [{"pid": shell, "argv0": "sh", "argv": ["sh"], "name": "sh", "cwd": info["cwd"]}]
print(json.dumps({"id": "1", "result": {"process_info": {
    "pane_id": pane, "shell_pid": shell, "foreground_process_group_id": foreground,
    "foreground_processes": processes}}}))
'''

PANE_SHELL = """#!/bin/sh
# A stand-in Herdr pane: a real shell that runs each line typed into its FIFO.
exec 3<>"$1"
while IFS= read -r line <&3; do
  eval "$line"
done
"""

STUB_TRUST = r'''#!{python}
import json, os, sys
from pathlib import Path

state = Path(os.environ["STUB_STATE"])
with (state / "trust.log").open("a") as log:
    log.write(json.dumps({"helper": sys.argv[0], "args": sys.argv[1:],
                          "config": os.environ.get("CLAUDE_CONFIG_DIR")}) + "\n")
trigger = state / "busy-on-trust"
if trigger.is_file():
    session = Path(trigger.read_text())
    data = json.loads(session.read_text())
    data["status"] = "busy"
    temporary = session.with_suffix(".trust-tmp")
    temporary.write_text(json.dumps(data))
    os.replace(temporary, session)
    trigger.unlink()
    (state / "trust-made-busy").write_text(str(session))
trigger = state / "invalid-busy-record-on-trust"
if trigger.is_file():
    busy_record = Path(trigger.read_text())
    busy_record.write_text("v1 gen=8 state=unexpected\n")
    trigger.unlink()
    (state / "trust-made-invalid-busy-record").write_text(str(busy_record))
'''

STUB_LOCK_LIBRARY = r'''# Stand-in for Firstmate's bin/fm-wake-lib.sh record lock.
fm_meta_lock_path() {
  case "$1" in */*.meta) ;; *) return 1 ;; esac
  local base=${1##*/}
  printf '%s/.meta-%s.lock\n' "${1%/*}" "${base%.meta}"
}
_stub_account() {
  local base=${1##*/}
  base=${base#.meta-}
  grep '^account=' "${1%/*}/${base%.lock}.meta" || echo account-unset
}
fm_lock_acquire_wait_bounded() {
  if [ -f "$STUB_STATE/fail-second-lock" ]; then
    count=$(cat "$STUB_STATE/lock-count" 2>/dev/null || echo 0)
    count=$((count + 1))
    printf '%s\n' "$count" >"$STUB_STATE/lock-count"
    [ "$count" -le 1 ] || return 1
  fi
  mkdir "$1" 2>/dev/null || return 1
  printf 'acquire %s %s %s\n' "$FM_HOME" "$1" "$(_stub_account "$1")" >>"$STUB_STATE/lock.log"
}
fm_lock_release() {
  printf 'release %s %s\n' "$1" "$(_stub_account "$1")" >>"$STUB_STATE/lock.log"
  rmdir "$1"
}
'''

RECORDING_STUB = """#!/bin/sh
echo "$(basename "$0") $*" >>"$STUB_STATE/forbidden.log"
exit 97
"""


def wait_for(condition, *, seconds: float = 15, what: str = "condition"):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def alive(pid: int) -> bool:
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def load_tool():
    loader = importlib.machinery.SourceFileLoader("account_swap_under_test", str(TOOL))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def files_holding(root: Path, needle: bytes) -> list[str]:
    return sorted(str(path) for path in root.rglob("*")
                  if path.is_file() and not path.is_symlink() and needle in path.read_bytes())


class Agent:
    def __init__(self, pid: int, sid: str, transcript: Path, pane: str, cwd: Path) -> None:
        self.pid, self.sid, self.transcript, self.pane, self.cwd = pid, sid, transcript, pane, cwd


class Sandbox:
    def __init__(self, test: unittest.TestCase) -> None:
        self.test = test
        self.root = Path(tempfile.mkdtemp(prefix="account-swap-")).resolve()
        test.addCleanup(self.close)
        self.home = self.root / "home"
        self.stubs = self.root / "stubs"
        self.state = self.root / "state"
        self.shells: list[subprocess.Popen] = []
        self.loose: list[subprocess.Popen] = []
        for folder in (self.stubs, self.state / "panes", self.state / "procs"):
            folder.mkdir(parents=True)
        python = sys.executable
        self.write_stub("claude", STUB_CLAUDE.replace("{python}", python))
        self.write_stub("herdr", STUB_HERDR.replace("{python}", python).replace("{session}", SESSION))
        for name in ("security", "launchctl", "osascript", "claude-swap"):
            self.write_stub(name, RECORDING_STUB)
        self.pane_shell = self.stubs / "pane-shell"
        self.pane_shell.write_text(PANE_SHELL)
        self.accounts = {1: self.home / ".claude", 2: self.home / ".claude-profiles/acct2"}
        for root in self.accounts.values():
            (root / "projects").mkdir(parents=True)
            (root / "settings.json").write_text('{"model": "untouched"}\n')
        self.main = self.home / ".harness/main/firstmate"
        self.domain = self.home / ".harness/homes/dom/firstmate"
        for home in (self.main, self.domain):
            (home / "state").mkdir(parents=True)
            (home / "config").mkdir()
            (home / "config/claude-account").write_text("ordinary\n")
            (home / "bin").mkdir()
            trust = home / "bin/fm-claude-trust.sh"
            trust.write_text(STUB_TRUST.replace("{python}", python))
            trust.chmod(0o755)
            (home / "bin/fm-wake-lib.sh").write_text(STUB_LOCK_LIBRARY)
        self.panes = 0

    def write_stub(self, name: str, text: str) -> None:
        path = self.stubs / name
        path.write_text(text)
        path.chmod(0o755)

    def close(self) -> None:
        for shell in self.shells:
            try:
                os.killpg(shell.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        for process in self.shells + self.loose:
            process.kill()
            process.wait()
        shutil.rmtree(self.root, ignore_errors=True)

    def environment(self) -> dict[str, str]:
        return {"HOME": str(self.home), "PATH": f"{self.stubs}:/usr/bin:/bin", "STUB_STATE": str(self.state),
                "TZ": "UTC", "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1"}

    def run(self, *arguments: str, constants: dict[str, int] | None = None) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, str(TOOL), *arguments]
        if constants:
            command = [sys.executable, "-c", CONSTANTS_RUNNER, str(TOOL), json.dumps(constants), *arguments]
        return subprocess.run(command, text=True, capture_output=True, env=self.environment(), timeout=180,
                              check=False)

    def open_pane(self, cwd: Path, workspace: str) -> str:
        self.panes += 1
        pane = f"{workspace}:p{self.panes}"
        fifo = self.state / f"pane-{self.panes}.fifo"
        os.mkfifo(fifo)
        output = (self.state / f"pane-{self.panes}.out").open("w")
        shell = subprocess.Popen(["/bin/sh", str(self.pane_shell), str(fifo)], cwd=cwd, stdin=subprocess.DEVNULL,
                                 stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        output.close()
        self.shells.append(shell)
        (self.state / "panes" / (pane.replace(":", "_") + ".json")).write_text(json.dumps({
            "shell": shell.pid, "fifo": str(fifo), "workspace": workspace, "tab": f"{workspace}:t1", "cwd": str(cwd)}))
        return pane

    def type_into(self, pane: str, line: str) -> None:
        info = json.loads((self.state / "panes" / (pane.replace(":", "_") + ".json")).read_text())

        def opened() -> int | None:
            try:
                return os.open(info["fifo"], os.O_WRONLY | os.O_NONBLOCK)
            except OSError as error:  # no reader yet: the pane shell is still starting
                if error.errno != errno.ENXIO:
                    raise
                return None

        descriptor = wait_for(opened, what=f"pane {pane} to read its input")
        os.write(descriptor, (line + "\n").encode())
        os.close(descriptor)

    def shell_of(self, pane: str) -> int:
        return json.loads((self.state / "panes" / (pane.replace(":", "_") + ".json")).read_text())["shell"]

    def pane_claude(self, pane: str) -> int | None:
        shell = str(self.shell_of(pane))
        rows = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,stat="], capture_output=True, text=True).stdout
        for row in rows.splitlines():
            pid, parent, state = row.split()[:3]
            if parent == shell and not state.startswith("Z") and (self.state / "procs" / f"{pid}.json").is_file():
                return int(pid)
        return None

    def start_agent(self, cwd: Path, *, account: int | Path | None, environment: dict[str, str],
                    argv: list[str] | None = None, workspace: str = "w1") -> Agent:
        cwd.mkdir(parents=True, exist_ok=True)
        pane = self.open_pane(cwd, workspace)
        values = {"HOME": str(self.home), "PATH": f"{self.stubs}:/usr/bin:/bin", "TZ": "UTC", "LC_ALL": "C",
                  "HERDR_SESSION": SESSION, "HERDR_PANE_ID": pane, "UNLISTED_MARKER": "not a carried name",
                  **environment}
        if isinstance(account, int):
            if account != 1:
                values["CLAUDE_CONFIG_DIR"] = str(self.accounts[account])
        elif account is not None:
            values["CLAUDE_CONFIG_DIR"] = str(account)
        argv = argv or ["claude", "--dangerously-skip-permissions", "--settings", '{"feedbackDrafts":"off"}',
                        "--append-system-prompt", "worker trust text", "--model", "opus", "--effort", "xhigh",
                        ": Firstmate operational input waiting"]
        script = self.state / f"start-{self.panes}.sh"
        script.write_text(f"cd {shlex.quote(str(cwd))} || exit 1\nexec /usr/bin/env -i "
                          + " ".join(shlex.quote(word) for word in [f"{key}={value}" for key, value in values.items()] + argv)
                          + "\n")
        self.type_into(pane, f"sh {shlex.quote(str(script))}")
        pid = wait_for(lambda: self.pane_claude(pane), what=f"the stub claude in pane {pane}")
        config = Path(values.get("CLAUDE_CONFIG_DIR", self.accounts[1]))
        record = wait_for(lambda: self.session_record(config, pid), what="the session record")
        transcript = wait_for(lambda: next(iter(config.glob(f"projects/*/{record['sessionId']}.jsonl")), None),
                              what="the transcript")
        return Agent(pid, record["sessionId"], transcript, pane, cwd)

    @staticmethod
    def session_record(config: Path, pid: int) -> dict | None:
        try:
            return json.loads((config / "sessions" / f"{pid}.json").read_text())
        except (OSError, ValueError):
            return None

    def worker_record(self, home: Path, task: str, agent: Agent, *, account: str | None) -> Path:
        workspace = agent.pane.split(":")[0]
        lines = [f"window={SESSION}:{agent.pane}", f"endpoint_task_id={task}", f"worktree={agent.cwd}",
                 f"project={home}/projects/sample", "harness=claude", "kind=ship", "effort=xhigh"]
        if account is not None:
            lines.append(f"account={account}")
        lines += ["backend=herdr", f"herdr_session={SESSION}", f"herdr_workspace_id={workspace}",
                  f"herdr_tab_id={workspace}:t1", f"herdr_pane_id={agent.pane}", "busy_gen=7"]
        meta = home / "state" / f"{task}.meta"
        meta.write_text("\n".join(lines) + "\n")
        return meta

    def lead_record(self, lead: str, home: Path, agent: Agent) -> Path:
        workspace = agent.pane.split(":")[0]
        meta = self.main / "state" / f"{lead}.meta"
        meta.write_text("\n".join([
            f"window={SESSION}:{agent.pane}", f"endpoint_task_id={lead}", "kind=secondmate", f"home={home}",
            f"projects={home}/projects", "harness=claude", "effort=high", "account=ordinary", "backend=herdr",
            f"herdr_session={SESSION}", f"herdr_workspace_id={workspace}", f"herdr_tab_id={workspace}:t1",
            f"herdr_pane_id={agent.pane}"]) + "\n")
        return meta

    def snapshot(self) -> dict[str, bytes | str]:
        found: dict[str, bytes | str] = {}
        for path in sorted(self.home.rglob("*")):
            relative = str(path.relative_to(self.home))
            if path.is_symlink():
                found[relative] = "link:" + os.readlink(path)
            elif path.is_file():
                found[relative] = path.read_bytes()
            else:
                found[relative] = "folder"
        return found

    def log(self, name: str) -> list:
        path = self.state / name
        if not path.exists():
            return []
        return [json.loads(line) if line.startswith(("{", "[")) else line
                for line in path.read_text().splitlines()]

    def herdr_commands(self) -> list[tuple[str, str]]:
        return [tuple(entry[:2]) for entry in self.log("herdr.log")]

    def claude_launch(self, pid: int) -> dict:
        return [entry for entry in self.log("claude.log") if entry["pid"] == pid][-1]

    def run_folder(self) -> Path:
        [folder] = (self.home / ".local/state/account-swap").glob("*-*")
        return folder


class AccountSwapTest(unittest.TestCase):
    def setUp(self) -> None:
        self.box = Sandbox(self)
        self.untouched = {path: path.read_bytes() for path in [
            *(root / "settings.json" for root in self.box.accounts.values()),
            *(home / "config/claude-account" for home in (self.box.main, self.box.domain))]}

    def tearDown(self) -> None:
        for path, content in self.untouched.items():
            self.assertEqual(path.read_bytes(), content, f"{path} must never be written")
        self.assertEqual(self.box.log("forbidden.log"), [], "credentials and login items are never touched")
        for command in self.box.herdr_commands():
            self.assertIn(command, {("pane", "process-info"), ("pane", "get"), ("pane", "run")})

    def worker(self, home: Path, task: str, *, account: int | Path = 2, record: bool = True,
               environment: dict[str, str] | None = None, argv: list[str] | None = None,
               workspace: str = "w1") -> Agent:
        agent = self.box.start_agent(self.box.root / "work" / task, account=account, argv=argv, workspace=workspace,
                                     environment={"FM_TASK_ID": task, **(environment or {})})
        if record:
            value = None if account == 1 else str(account if isinstance(account, Path) else self.box.accounts[account])
            self.box.worker_record(home, task, agent, account="ordinary" if account == 1 else value)
        return agent

    def assert_still_running(self, agent: Agent, account: int) -> None:
        self.assertTrue(alive(agent.pid), "the agent must be left running")
        self.assertEqual(self.box.pane_claude(agent.pane), agent.pid)
        other = self.box.accounts[2 if account == 1 else 1]
        self.assertEqual(list(other.glob(f"projects/*/{agent.sid}*")), [], "nothing may be copied")

    def assert_resumed_on(self, agent: Agent, account: int) -> int:
        new_pid = wait_for(lambda: (pid := self.box.pane_claude(agent.pane)) != agent.pid and pid,
                           what="the resumed claude")
        record = wait_for(lambda: self.box.session_record(self.box.accounts[account], new_pid),
                          what="the resumed session record")
        self.assertEqual(record["sessionId"], agent.sid, "the same conversation id is resumed")
        return new_pid

    def outcome_line(self, output: str, result: str, label: str) -> str:
        lines = [line.strip() for line in output.splitlines() if line.strip().startswith(f"{result} {label} ")]
        self.assertEqual(len(lines), 1, output)
        return lines[0]

    # The plan.

    def test_plan_lists_every_agent_and_changes_nothing(self) -> None:
        lead = self.box.start_agent(self.box.domain, account=2, environment={"FM_HOME": str(self.box.domain)},
                                    workspace="w9")
        self.box.lead_record("dom", self.box.domain, lead)
        ready = self.worker(self.box.domain, "t1")
        flagged = self.worker(self.box.domain, "t4")
        flagged_meta = self.box.domain / "state/t4.meta"
        flagged_meta.with_suffix(".busy-state").write_text(
            "v1 gen=7 seq=2 state=busy source=claude-hook event=user-prompt-submit ts=1\n")
        elsewhere = self.worker(self.box.main, "t2", account=1)
        stale = self.box.main / "state/t3.meta"
        stale.write_text(f"harness=claude\nkind=ship\nbackend=herdr\nherdr_session={SESSION}\nherdr_pane_id=w7:p99\n")
        loose = self.box.start_agent(self.box.root / "work/loose", account=2, environment={}, workspace="w5")
        before = self.box.snapshot()

        result = self.box.run("all", "--from", "2", "--to", "1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = result.stdout.splitlines()
        self.assertIn("plan: every recorded agent on account 2, to account 1", lines[0])
        header = next(line for line in lines if line.strip().startswith("agent"))
        for column in ("home", "on now", "conversation", "moves to", "plan"):
            self.assertIn(column, header)
        rows = {line.split()[1]: line for line in lines if line.strip().startswith(("lead ", "worker "))}
        self.assertEqual(set(rows), {"dom", "t1", "t4"})
        self.assertLess(lines.index(rows["dom"]), lines.index(rows["t1"]), "leads are listed first")
        for name, agent in (("dom", lead), ("t1", ready), ("t4", flagged)):
            with self.subTest(agent=name):
                self.assertIn(agent.sid, rows[name])
                self.assertRegex(rows[name], r"\saccount 2\s+" + agent.sid + r"\s+account 1\s")
                self.assertIn(" dom ", rows[name])
        self.assertTrue(rows["t1"].rstrip().endswith("move"))
        self.assertIn("wait: its Firstmate busy record says it is mid-turn; --go waits up to 240 s for its next "
                      "pause, then leaves it", rows["t4"])
        self.assertNotIn(elsewhere.sid, result.stdout)
        self.assertIn(f"also on account 2, not moved (no Firstmate worker or lead record): pid {loose.pid}",
                      result.stdout)
        self.assertIn("skipped 1 Firstmate records", result.stdout)
        self.assertIn("dry run: nothing was stopped, copied or written.", result.stdout)

        single = self.box.run("worker", "t1", "--to", "1")
        self.assertEqual(single.returncode, 0, single.stderr)
        self.assertIn("plan: one worker, to account 1", single.stdout)
        self.assertIn(ready.sid, single.stdout)

        self.assertEqual(self.box.snapshot(), before, "a plan run writes nothing")
        for agent in (lead, ready, flagged, elsewhere, loose):
            self.assertTrue(alive(agent.pid))
        self.assertEqual({command for command in self.box.herdr_commands()}, {("pane", "process-info")})
        self.assertEqual(self.box.log("trust.log"), [])
        self.assertEqual(self.box.log("lock.log"), [])

    # Refusals.

    def test_protected_conversations_are_refused_and_never_named(self) -> None:
        short_sid, long_sid = str(uuid.uuid4()), str(uuid.uuid4())
        listed_file = self.box.home / ".config/account-swap/protected-conversations"
        listed_file.parent.mkdir(parents=True)
        listed_file.write_text(f"# conversations that hold secrets\n{short_sid[:8].upper()}\n"
                               f"{long_sid[:13]}  # a longer prefix\n")
        by_short = self.worker(self.box.domain, "p1", environment={"STUB_SESSION_ID": short_sid})
        by_long = self.worker(self.box.domain, "p2", environment={"STUB_SESSION_ID": long_sid})

        result = self.box.run("all", "--from", "2", "--to", "1", "--go")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        for agent in (by_short, by_long):
            with self.subTest(sid=agent.sid[:8]):
                self.assertIn(f"{agent.sid[:8]}... (protected)", result.stdout)
                self.assertNotIn(agent.sid, result.stdout + result.stderr)
                self.assert_still_running(agent, 2)
        self.assertEqual(result.stdout.count("refused: protected conversation"), 2)
        self.assertIn("nothing to move.", result.stdout)
        self.assertNotIn(("pane", "run"), self.box.herdr_commands())
        self.assertFalse((self.box.home / ".local/state/account-swap").exists())

    def test_an_invalid_protected_list_refuses_before_naming_a_conversation(self) -> None:
        agent = self.worker(self.box.domain, "p3")
        listed_file = self.box.home / ".config/account-swap/protected-conversations"
        listed_file.parent.mkdir(parents=True)
        listed_file.write_bytes(b"12345678-abcd\n\xff\n")

        result = self.box.run("worker", "p3", "--to", "1", "--go")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(str(listed_file), result.stderr)
        self.assertIn("line 2", result.stderr)
        self.assertNotIn(agent.sid, result.stdout + result.stderr)
        self.assertNotIn("plan:", result.stdout)
        self.assert_still_running(agent, 2)
        self.assertFalse((self.box.home / ".local/state/account-swap").exists())

    def test_a_malformed_ascii_protected_prefix_refuses_before_the_plan(self) -> None:
        agent = self.worker(self.box.domain, "p4")
        listed_file = self.box.home / ".config/account-swap/protected-conversations"
        listed_file.parent.mkdir(parents=True)
        listed_file.write_text("12345678-abcd\n123456789\n")

        result = self.box.run("worker", "p4", "--to", "1", "--go")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(str(listed_file), result.stderr)
        self.assertIn("line 2", result.stderr)
        self.assertNotIn("123456789", result.stdout + result.stderr)
        self.assertNotIn(agent.sid, result.stdout + result.stderr)
        self.assertNotIn("plan:", result.stdout)
        self.assert_still_running(agent, 2)
        self.assertFalse((self.box.home / ".local/state/account-swap").exists())

    def test_a_conversation_open_in_another_process_is_refused(self) -> None:
        agent = self.worker(self.box.domain, "o1")
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "--resume", agent.sid],
                                  start_new_session=True)
        self.box.loose.append(holder)
        wait_for(lambda: agent.sid in subprocess.run(["ps", "-ww", "-o", "args=", "-p", str(holder.pid)],
                                                     capture_output=True, text=True).stdout, what="the holder")

        result = self.box.run("worker", "o1", "--to", "1", "--go")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"refused: its conversation is also open in pid {holder.pid}", result.stdout)
        self.assert_still_running(agent, 2)
        self.assertNotIn(("pane", "run"), self.box.herdr_commands())

    def test_unknown_accounts_and_unresumable_launches_are_refused(self) -> None:
        cases = {
            "u1": ({"account": self.box.home / ".claude-profiles/other"},
                   "CLAUDE_CONFIG_DIR set to a folder that is not one of this tool's accounts"),
            "u2": ({"account": self.box.accounts[1]}, "a login separate from account 1"),
            "u3": ({"argv": ["claude", "--continue"]}, "it was started with --continue"),
            "u4": ({"argv": ["claude", "--model", "opus", "first prompt", "second prompt"]},
                   "more than one prompt argument"),
        }
        agents = {task: self.worker(self.box.domain, task, **options) for task, (options, _) in cases.items()}
        for task, (_, reason) in cases.items():
            with self.subTest(task=task):
                result = self.box.run("worker", task, "--to", "1", "--go")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("refused: ", result.stdout)
                self.assertIn(reason, result.stdout)
                self.assertTrue(alive(agents[task].pid))
        self.assertNotIn(("pane", "run"), self.box.herdr_commands())
        self.assertEqual(self.box.log("trust.log"), [])

    def test_an_unknown_account_folder_is_refused_without_disclosing_its_value(self) -> None:
        sentinel = str(self.box.root / f"SENTINEL-private-account-{uuid.uuid4().hex}")
        agent = self.box.start_agent(self.box.root / "work/u6", account=Path(sentinel),
                                     environment={"FM_TASK_ID": "u6"})
        self.box.worker_record(self.box.domain, "u6", agent, account=str(self.box.accounts[2]))

        result = self.box.run("worker", "u6", "--to", "1", "--go")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("refused: it runs with CLAUDE_CONFIG_DIR set to a folder that is not one of "
                      "this tool's accounts", result.stdout)
        self.assertNotIn(sentinel, result.stdout + result.stderr)
        self.assertEqual(files_holding(self.box.home, sentinel.encode()), [])
        self.assert_still_running(agent, 2)
        self.assertNotIn(("pane", "run"), self.box.herdr_commands())
        self.assertFalse((self.box.home / ".local/state/account-swap").exists())

    def test_an_environment_that_can_override_the_account_is_refused_by_name_only(self) -> None:
        def secret(name: str) -> str:
            return f"SENTINEL-{name}-{uuid.uuid4().hex}"

        cases = {
            "v1": ({"environment": {"ANTHROPIC_AUTH_TOKEN": secret("v1")}},
                   "its environment sets ANTHROPIC_AUTH_TOKEN, which can override the account it runs on"),
            "v2": ({"environment": {"CLAUDE_CODE_OAUTH_TOKEN": secret("v2")}},
                   "its environment sets CLAUDE_CODE_OAUTH_TOKEN, which can override"),
            "v3": ({"environment": {"ANTHROPIC_BASE_URL": secret("v3")}},
                   "its environment sets ANTHROPIC_BASE_URL, which can override"),
            "v4": ({"environment": {"CLAUDE_CODE_USE_BEDROCK": secret("v4")}},
                   "its environment sets CLAUDE_CODE_USE_BEDROCK, which can override"),
            "v5": ({"argv": ["claude", "--settings", json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": secret("v5")}}),
                             "--model", "opus"]},
                   "its --settings value sets environment variables (ANTHROPIC_AUTH_TOKEN)"),
            "v6": ({"environment": {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
                                    "GIT_CONFIG_VALUE_0": "Authorization: " + secret("v6")}},
                   "its environment sets git configuration this tool does not carry onto a resume "
                   "(GIT_CONFIG_KEY_0)"),
        }
        agents = {task: self.worker(self.box.domain, task, **options) for task, (options, _) in cases.items()}
        sentinels = {match.encode() for entry in self.box.log("claude.log")
                     for match in re.findall(r"SENTINEL-v\d-[0-9a-f]{32}", json.dumps(entry))}
        self.assertEqual(len(sentinels), len(cases))
        before = self.box.snapshot()

        result = self.box.run("all", "--from", "2", "--to", "1", "--go")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("nothing to move.", result.stdout)
        for task, (_, reason) in cases.items():
            with self.subTest(task=task):
                row = next(line for line in result.stdout.splitlines() if line.strip().startswith(f"worker {task} "))
                self.assertIn("refused: " + reason, row)
                self.assert_still_running(agents[task], 2)
        for sentinel in sentinels:
            self.assertNotIn(sentinel.decode(), result.stdout + result.stderr, "a value is never printed")
        self.assertEqual(self.box.snapshot(), before, "a refused run writes nothing, so no value is written")
        self.assertNotIn(("pane", "run"), self.box.herdr_commands())
        self.assertEqual(self.box.log("trust.log"), [])
        self.assertFalse((self.box.home / ".local/state/account-swap").exists())

    def test_a_settings_file_is_refused_without_reading_or_copying_it(self) -> None:
        settings = self.box.state / "private-settings"
        os.mkfifo(settings)
        agent = self.worker(self.box.domain, "v7", argv=["claude", "--settings", str(settings), "prompt"])

        result = self.box.run("worker", "v7", "--to", "1", "--go")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("refused: its --settings names a file whose contents this tool cannot vouch for", result.stdout)
        self.assertNotIn(str(settings), result.stdout + result.stderr)
        self.assert_still_running(agent, 2)
        self.assertFalse((self.box.home / ".local/state/account-swap").exists())

    def test_settings_credential_keys_are_refused_by_key_only(self) -> None:
        keys = ("apiKeyHelper", "awsAuthRefresh", "awsCredentialExport")
        agents = {}
        values = {}
        for index, key in enumerate(keys, start=8):
            value = f"SENTINEL-{key}-{uuid.uuid4().hex}"
            values[key] = value
            agents[key] = self.worker(
                self.box.domain, f"v{index}",
                argv=["claude", "--settings", json.dumps({key: value, "feedbackDrafts": "off"}), "prompt"])

        result = self.box.run("all", "--from", "2", "--to", "1", "--go")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        for key, agent in agents.items():
            with self.subTest(key=key):
                self.assertIn(f"refused: its --settings value sets credential key {key}", result.stdout)
                self.assertNotIn(values[key], result.stdout + result.stderr)
                self.assert_still_running(agent, 2)
        self.assertFalse((self.box.home / ".local/state/account-swap").exists())

    def test_inline_mcp_config_credentials_are_refused_without_persisting_values(self) -> None:
        sentinel = f"SENTINEL-mcp-{uuid.uuid4().hex}"
        config = json.dumps({"mcpServers": {"service": {"command": "helper", "env": {"TOKEN": sentinel}}}})
        agent = self.worker(self.box.domain, "m1", argv=["claude", f"--mcp-config={config}", "prompt"])

        result = self.box.run("worker", "m1", "--to", "1", "--go")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("refused: its inline --mcp-config value can carry credentials "
                      "(env, headers, args or url)", result.stdout)
        self.assertNotIn(sentinel, result.stdout + result.stderr)
        self.assertEqual(files_holding(self.box.home, sentinel.encode()), [])
        self.assert_still_running(agent, 2)
        self.assertFalse((self.box.home / ".local/state/account-swap").exists())

    def test_a_path_mcp_config_is_carried_without_being_opened(self) -> None:
        config = self.box.state / "mcp-config"
        os.mkfifo(config)
        agent = self.worker(self.box.domain, "m2", argv=["claude", "--mcp-config", str(config), "prompt"])

        result = self.box.run("worker", "m2", "--to", "1", "--go")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        new_pid = self.assert_resumed_on(agent, 1)
        resumed = self.box.claude_launch(new_pid)["argv"]
        index = resumed.index("--mcp-config")
        self.assertEqual(resumed[index + 1], str(config))

    def test_override_names_and_carried_names_never_overlap(self) -> None:
        tool = load_tool()
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                     "CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL",
                     "ANTHROPIC_VERTEX_BASE_URL", "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_FOUNDRY_BASE_URL",
                     "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_PROFILE", "ANTHROPIC_FEDERATION_RULE_ID",
                     "AWS_BEARER_TOKEN_BEDROCK", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                     "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_ANTHROPIC_AWS", "CLAUDE_CODE_USE_MANTLE"):
            with self.subTest(name=name):
                self.assertEqual(tool.account_overrides({name: ""}), [name])
        for name in ("ANTHROPIC_MODEL", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_EFFORT_LEVEL", "GITHUB_TOKEN", "HOME"):
            with self.subTest(name=name):
                self.assertEqual(tool.account_overrides({name: ""}), [])
        self.assertEqual(tool.account_overrides(dict.fromkeys(tool.CARRIED, "")), [])
        self.assertNotIn("CLAUDE_CONFIG_DIR", tool.CARRIED, "the target account sets it")

    def test_a_session_record_without_process_start_is_refused(self) -> None:
        agent = self.worker(self.box.domain, "r1")
        session_path = self.box.accounts[2] / "sessions" / f"{agent.pid}.json"
        session = json.loads(session_path.read_text())
        del session["procStart"]
        session_path.write_text(json.dumps(session))

        result = self.box.run("worker", "r1", "--to", "1", "--go")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("refused: its session record belongs to an earlier process with the same number", result.stdout)
        self.assert_still_running(agent, 2)
        self.assertNotIn(("pane", "run"), self.box.herdr_commands())
        self.assertFalse((self.box.home / ".local/state/account-swap").exists())

    def test_a_repointed_record_is_refused_during_the_busy_wait(self) -> None:
        agent = self.worker(self.box.domain, "b0", environment={"STUB_STATUS": "busy"})
        meta = self.box.domain / "state/b0.meta"
        command = [sys.executable, "-c", CONSTANTS_RUNNER, str(TOOL),
                   json.dumps({"PAUSE_SECONDS": 4, "BUSY_RECHECK_SECONDS": 1}),
                   "worker", "b0", "--to", "1", "--go"]
        process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=self.box.environment())
        output = []
        while True:
            line = process.stdout.readline()
            self.assertTrue(line, "account-swap exited before entering the busy wait")
            output.append(line)
            if "waiting: Claude reports it is mid-turn" in line:
                break
        meta.write_text(meta.read_text().replace(f"herdr_pane_id={agent.pane}", "herdr_pane_id=w9:p99"))
        stdout, stderr = process.communicate(timeout=30)
        output.append(stdout)
        text = "".join(output)

        self.assertEqual(process.returncode, 1, text + stderr)
        self.assertIn("not moved: its Firstmate record points to a different endpoint", text)
        self.assert_still_running(agent, 2)
        self.assertNotIn(("pane", "run"), self.box.herdr_commands())
        self.assertEqual(self.box.log("trust.log"), [])

    def test_an_agent_that_becomes_busy_before_stop_waits_then_moves_without_setting_its_copy_aside(self) -> None:
        agent = self.worker(self.box.domain, "b2")
        session_path = self.box.accounts[2] / "sessions" / f"{agent.pid}.json"
        (self.box.state / "busy-on-trust").write_text(str(session_path))
        command = [sys.executable, "-c", CONSTANTS_RUNNER, str(TOOL),
                   json.dumps({"PAUSE_SECONDS": 8, "BUSY_RECHECK_SECONDS": 1}),
                   "worker", "b2", "--to", "1", "--go"]
        process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=self.box.environment())
        wait_for(lambda: (self.box.state / "trust-made-busy").is_file(), what="trust to mark the session busy")
        target = self.box.accounts[1] / "projects" / agent.transcript.parent.name / f"{agent.sid}.jsonl"
        wait_for(target.is_file, what="the first verified target copy")
        output = []
        while True:
            line = process.stdout.readline()
            self.assertTrue(line, "account-swap exited before waiting on the newly busy agent")
            output.append(line)
            if "waiting: Claude reports it is mid-turn" in line:
                break

        self.assertEqual(json.loads(session_path.read_text())["status"], "busy")
        self.assertTrue(alive(agent.pid), "a newly busy agent is not signalled")
        self.assertNotIn(("pane", "run"), self.box.herdr_commands())
        session = json.loads(session_path.read_text())
        session["status"] = "idle"
        temporary = session_path.with_suffix(".idle-tmp")
        temporary.write_text(json.dumps(session))
        os.replace(temporary, session_path)
        stdout, stderr = process.communicate(timeout=60)
        output.append(stdout)
        text = "".join(output)

        self.assertEqual(process.returncode, 0, text + stderr)
        self.assert_resumed_on(agent, 1)
        self.assertIn("waiting: Claude reports it is mid-turn", text)
        self.assertEqual(list(target.parent.glob(f"{agent.sid}.jsonl.older-copy-*")), [])

    def assert_invalid_busy_record_is_treated_as_busy(self, task: str, prepare) -> None:
        agent = self.worker(self.box.domain, task)
        busy_record = (self.box.domain / f"state/{task}.meta").with_suffix(".busy-state")
        prepare(busy_record)

        result = self.box.run("worker", task, "--to", "1", "--go", constants={"PAUSE_SECONDS": 1})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("its Firstmate busy record says it is mid-turn", result.stdout)
        self.assertIn("not moved: still busy after 1 s", result.stdout)
        self.assert_still_running(agent, 2)
        self.assertNotIn(("pane", "run"), self.box.herdr_commands())

    def test_an_unreadable_busy_record_is_treated_as_busy(self) -> None:
        def prepare(path: Path) -> None:
            path.write_text("v1 state=idle\n")
            path.chmod(0)

        self.assert_invalid_busy_record_is_treated_as_busy("i1", prepare)

    def test_a_non_utf8_busy_record_is_treated_as_busy(self) -> None:
        self.assert_invalid_busy_record_is_treated_as_busy("i2", lambda path: path.write_bytes(b"v1 state=\xff\n"))

    def test_a_malformed_busy_record_is_treated_as_busy(self) -> None:
        self.assert_invalid_busy_record_is_treated_as_busy(
            "i3", lambda path: path.write_text("v1 gen=4 malformed state=idle\n"))

    def test_a_busy_record_missing_state_is_treated_as_busy(self) -> None:
        self.assert_invalid_busy_record_is_treated_as_busy("i4", lambda path: path.write_text("v1 gen=4 seq=2\n"))

    def test_a_busy_record_with_an_unknown_state_is_treated_as_busy(self) -> None:
        self.assert_invalid_busy_record_is_treated_as_busy(
            "i5", lambda path: path.write_text("v1 gen=4 state=unexpected\n"))

    def test_an_invalid_busy_record_at_the_final_check_waits_without_signalling(self) -> None:
        agent = self.worker(self.box.domain, "i6")
        busy_record = (self.box.domain / "state/i6.meta").with_suffix(".busy-state")
        (self.box.state / "invalid-busy-record-on-trust").write_text(str(busy_record))
        command = [sys.executable, "-c", CONSTANTS_RUNNER, str(TOOL),
                   json.dumps({"PAUSE_SECONDS": 8, "BUSY_RECHECK_SECONDS": 1}),
                   "worker", "i6", "--to", "1", "--go"]
        process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=self.box.environment())
        wait_for(lambda: (self.box.state / "trust-made-invalid-busy-record").is_file(),
                 what="trust to make the busy record invalid")
        output = []
        while True:
            line = process.stdout.readline()
            self.assertTrue(line, "account-swap exited before waiting on the invalid busy record")
            output.append(line)
            if "waiting: its Firstmate busy record says it is mid-turn" in line:
                break

        self.assertTrue(alive(agent.pid), "an agent with an invalid busy record is not signalled")
        self.assertNotIn(("pane", "run"), self.box.herdr_commands())
        busy_record.unlink()
        stdout, stderr = process.communicate(timeout=60)
        output.append(stdout)
        text = "".join(output)

        self.assertEqual(process.returncode, 0, text + stderr)
        self.assert_resumed_on(agent, 1)

    def test_a_busy_agent_is_left_running_after_the_wait(self) -> None:
        agent = self.worker(self.box.domain, "b1", environment={"STUB_STATUS": "busy"})
        plan = self.box.run("worker", "b1", "--to", "1")
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertIn("wait: Claude reports it is mid-turn; --go waits up to 240 s for its next pause", plan.stdout)

        result = self.box.run("worker", "b1", "--to", "1", "--go", constants={"PAUSE_SECONDS": 1})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("not moved: still busy after 1 s (Claude reports it is mid-turn); it is still running as it was",
                      result.stdout)
        self.assert_still_running(agent, 2)
        self.assertNotIn(("pane", "run"), self.box.herdr_commands())
        self.assertEqual(self.box.log("trust.log"), [])

    def test_missing_or_malformed_herdr_record_endpoints_refuse_before_copy(self) -> None:
        cases = (("e1", "workspace", None, "Herdr gave no valid workspace_id"),
                 ("e2", "tab", "not valid!", "Herdr gave no valid tab_id"))
        for task, field, value, reason in cases:
            agent = self.worker(self.box.domain, task)
            pane_file = self.box.state / "panes" / (agent.pane.replace(":", "_") + ".json")
            pane = json.loads(pane_file.read_text())
            if value is None:
                del pane[field]
            else:
                pane[field] = value
            pane_file.write_text(json.dumps(pane))

            result = self.box.run("worker", task, "--to", "1", "--go")
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("not moved: " + reason, result.stdout)
            self.assert_still_running(agent, 2)
        self.assertNotIn(("pane", "run"), self.box.herdr_commands())
        self.assertEqual(self.box.log("trust.log"), [])

    def test_a_record_missing_an_endpoint_field_refuses_before_copy(self) -> None:
        agent = self.worker(self.box.domain, "e3")
        meta = self.box.domain / "state/e3.meta"
        meta.write_text("\n".join(line for line in meta.read_text().splitlines()
                                  if not line.startswith("herdr_workspace_id=")) + "\n")

        result = self.box.run("worker", "e3", "--to", "1", "--go")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("not moved: its Firstmate record lacks herdr_workspace_id", result.stdout)
        self.assert_still_running(agent, 2)
        self.assertEqual(self.box.log("trust.log"), [])

    def test_an_unavailable_record_lock_refuses_before_copy(self) -> None:
        agent = self.worker(self.box.domain, "e4")
        (self.box.domain / "bin/fm-wake-lib.sh").unlink()

        result = self.box.run("worker", "e4", "--to", "1", "--go")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("not moved: Firstmate's record lock library", result.stdout)
        self.assert_still_running(agent, 2)
        self.assertEqual(self.box.log("trust.log"), [])

    # Moves.

    def test_worker_moves_with_its_exact_conversation(self) -> None:
        hooks = str(self.box.root / "fm-hooks")
        agent = self.worker(self.box.domain, "t1", workspace="w1", environment={
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": hooks})
        meta = self.box.domain / "state/t1.meta"
        original_record = meta.read_bytes()
        slug = agent.transcript.parent.name
        side = self.box.accounts[2] / "projects" / slug / agent.sid
        (side / "subagents").mkdir(parents=True)
        (side / "subagents/agent-1.jsonl").write_text('{"side": true}\n')
        history = self.box.accounts[2] / "file-history" / agent.sid
        history.mkdir(parents=True)
        (history / "abc@v1").write_bytes(b"\x00\x01 history bytes")
        stale = self.box.accounts[1] / "projects" / slug / f"{agent.sid}.jsonl"
        stale.parent.mkdir(parents=True)
        stale.write_text("an older copy\n")
        # Herdr moved the pane to another workspace since the record was written.
        pane_file = self.box.state / "panes" / (agent.pane.replace(":", "_") + ".json")
        pane_info = json.loads(pane_file.read_text())
        pane_file.write_text(json.dumps({**pane_info, "workspace": "w8", "tab": "w8:t3"}))
        launched = self.box.claude_launch(agent.pid)

        result = self.box.run("worker", "t1", "--home", "dom", "--to", "1", "--go")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        new_pid = self.box.pane_claude(agent.pane)
        self.assertIsNotNone(new_pid)
        self.assertNotEqual(new_pid, agent.pid)
        self.assertFalse(alive(agent.pid))
        record = self.box.session_record(self.box.accounts[1], new_pid)
        self.assertEqual(record["sessionId"], agent.sid, "the same conversation id is resumed")
        target = self.box.accounts[1] / "projects" / slug / f"{agent.sid}.jsonl"
        self.assertIn(f"moved: pid {agent.pid} on account 2 -> pid {new_pid} on account 1 in pane {agent.pane}; "
                      f"conversation {agent.sid} kept; transcript {target}", result.stdout)
        self.assertIn("final copy verified byte for byte, both ways", result.stdout)

        # The source is left in place, unchanged since the stop.
        source = agent.transcript.read_bytes()
        self.assertTrue(source.rstrip().endswith(b'"type": "end", "pid": ' + str(agent.pid).encode() + b"}"))
        self.assertTrue(target.read_bytes().startswith(source), "the target continues the exact transcript")
        self.assertIn(b'"type": "start", "pid": ' + str(new_pid).encode(), target.read_bytes()[len(source):])
        self.assertEqual((self.box.accounts[1] / "projects" / slug / agent.sid / "subagents/agent-1.jsonl").read_text(),
                         '{"side": true}\n')
        self.assertEqual((self.box.accounts[1] / "file-history" / agent.sid / "abc@v1").read_bytes(),
                         b"\x00\x01 history bytes")
        self.assertTrue((side / "subagents/agent-1.jsonl").exists())
        self.assertTrue((history / "abc@v1").exists())
        [aside] = stale.parent.glob(f"{agent.sid}.jsonl.older-copy-*")
        self.assertEqual(aside.read_text(), "an older copy\n")
        self.assertIn(f"an older copy on account 1 was set aside as {aside}", result.stdout)

        # The same launch, resumed: the first prompt is dropped, the flags are
        # kept, and only the carried environment names arrive, with the account
        # the one thing that changes.
        resumed = self.box.claude_launch(new_pid)
        self.assertEqual(resumed["argv"][:-3], launched["argv"][:-1])
        self.assertEqual(resumed["argv"][-3:-1], ["--resume", agent.sid])
        self.assertIn("moved from Claude account 2 to account 1", resumed["argv"][-1])
        self.assertEqual(resumed["config"], str(self.box.accounts[1]))
        expected = {key: value for key, value in launched["env"].items()
                    if key not in ("CLAUDE_CONFIG_DIR", "UNLISTED_MARKER")}
        self.assertEqual(resumed["env"], expected)
        self.assertEqual(resumed["env"]["GIT_CONFIG_VALUE_0"], hooks, "Firstmate's commit hooks stay in place")

        # Firstmate's trust registration ran for the target account.
        [trust] = self.box.log("trust.log")
        self.assertEqual(trust["helper"], str(self.box.domain / "bin/fm-claude-trust.sh"))
        self.assertEqual(trust["args"], [str(agent.cwd), f"{self.box.domain}/projects/sample"])
        self.assertIsNone(trust["config"])

        # The record is updated under Firstmate's lock, with a dated copy kept.
        lines = meta.read_text().splitlines()
        self.assertIn("account=ordinary", lines)
        self.assertEqual(lines[lines.index("effort=xhigh") + 1], "account=ordinary")
        self.assertIn("herdr_workspace_id=w8", lines)
        self.assertIn("herdr_tab_id=w8:t3", lines)
        self.assertIn(f"herdr_pane_id={agent.pane}", lines)
        unchanged = [line for line in original_record.decode().splitlines()
                     if not line.startswith(("account=", "herdr_workspace_id=", "herdr_tab_id="))]
        self.assertEqual([line for line in lines if not line.startswith(
            ("account=", "herdr_workspace_id=", "herdr_tab_id="))], unchanged)
        [backup] = meta.parent.glob("t1.meta.pre-account-swap-*")
        self.assertEqual(backup.read_bytes(), original_record)
        lock = f"{self.box.domain}/state/.meta-t1.lock"
        self.assertEqual(self.box.log("lock.log"), [
            f"acquire {self.box.domain} {lock} account={self.box.accounts[2]}",
            f"release {lock} account={self.box.accounts[2]}",
            f"acquire {self.box.domain} {lock} account={self.box.accounts[2]}",
            f"release {lock} account=ordinary"])
        self.assertIn(f"record updated: account=ordinary and herdr_workspace_id=w8, herdr_tab_id=w8:t3; "
                      f"the previous record is kept as {backup.name}", result.stdout)

        # The run keeps its resume commands, private to the user.
        run_folder = self.box.run_folder()
        self.assertEqual(run_folder.stat().st_mode & 0o777, 0o700)
        launchers = sorted(path.name for path in run_folder.glob("*.sh"))
        self.assertEqual(launchers, ["worker-t1.back-to-account-2.sh", "worker-t1.to-account-1.sh"])
        for launcher in run_folder.glob("*.sh"):
            self.assertEqual(launcher.stat().st_mode & 0o777, 0o700)
        self.assertIn("summary: 1 moved", (run_folder / "log").read_text())

        again = self.box.run("worker", "t1", "--to", "1", "--go")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("nothing to move.", again.stdout)
        self.assertEqual(self.box.pane_claude(agent.pane), new_pid)

    def test_a_secret_in_the_environment_never_reaches_a_written_file(self) -> None:
        # Regression: the resume commands were written with the agent's whole
        # environment, so a token in it landed in ~/.local/state in plain text.
        secrets = {name: f"SENTINEL-{name}-{uuid.uuid4().hex}"
                   for name in ("GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "OPENAI_API_KEY", "NPM_TOKEN")}
        agent = self.worker(self.box.domain, "s1", environment=secrets)
        self.assertEqual({name: self.box.claude_launch(agent.pid)["env"][name] for name in secrets}, secrets)

        result = self.box.run("worker", "s1", "--to", "1", "--go")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        new_pid = self.assert_resumed_on(agent, 1)
        self.assertEqual(sorted(set(secrets) & set(self.box.claude_launch(new_pid)["env"])), [],
                         "the resumed agent does not receive them")
        self.assertEqual(len(list(self.box.run_folder().glob("*.sh"))), 2, "both resume commands were written")
        for name, value in secrets.items():
            with self.subTest(name=name):
                self.assertNotIn(value, result.stdout + result.stderr)
                self.assertEqual(files_holding(self.box.home, value.encode()), [])
                # The stand-ins' own files: only the test's start script and the
                # stub's record of the first launch ever held the value.
                self.assertEqual([Path(path).name for path in files_holding(self.box.state, value.encode())],
                                 ["claude.log", "start-1.sh"])

    def test_lead_moves_with_its_exact_conversation(self) -> None:
        lead = self.box.start_agent(self.box.domain, account=1, environment={"FM_HOME": str(self.box.domain)},
                                    workspace="w9")
        meta = self.box.lead_record("dom", self.box.domain, lead)
        original_record = meta.read_bytes()

        result = self.box.run("lead", "dom", "--to", "2", "--go")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        new_pid = self.box.pane_claude(lead.pane)
        self.assertNotEqual(new_pid, lead.pid)
        self.assertEqual(self.box.session_record(self.box.accounts[2], new_pid)["sessionId"], lead.sid)
        self.assertTrue((self.box.accounts[2] / "projects" / lead.transcript.parent.name / f"{lead.sid}.jsonl").is_file())
        self.assertIn(f"moved: pid {lead.pid} on account 1 -> pid {new_pid} on account 2", result.stdout)
        resumed = self.box.claude_launch(new_pid)
        self.assertEqual(resumed["argv"][-3:-1], ["--resume", lead.sid])
        self.assertIn("moved from Claude account 1 to account 2", resumed["argv"][-1])
        self.assertEqual(resumed["env"]["CLAUDE_CONFIG_DIR"], str(self.box.accounts[2]))
        self.assertEqual(resumed["env"]["FM_HOME"], str(self.box.domain))

        [trust] = self.box.log("trust.log")
        self.assertEqual(trust["helper"], str(self.box.main / "bin/fm-claude-trust.sh"))
        self.assertEqual(trust["args"], ["--secondmate-home", str(self.box.domain), "dom"])
        self.assertEqual(trust["config"], str(self.box.accounts[2]))

        self.assertIn(f"account={self.box.accounts[2]}", meta.read_text().splitlines())
        self.assertNotIn("account=ordinary", meta.read_text())
        [backup] = meta.parent.glob("dom.meta.pre-account-swap-*")
        self.assertEqual(backup.read_bytes(), original_record)
        self.assertEqual(self.box.log("lock.log")[0].split()[1], str(self.box.main))

    # Failures after the stop signal.

    def test_a_failed_final_record_write_reports_target_and_exact_repair(self) -> None:
        agent = self.worker(self.box.domain, "f0")
        meta = self.box.domain / "state/f0.meta"
        original = meta.read_bytes()
        (self.box.state / "fail-second-lock").write_text("fail the final record lock\n")

        result = self.box.run("worker", "f0", "--to", "1", "--go")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        new_pid = self.assert_resumed_on(agent, 1)
        line = self.outcome_line(result.stdout, "failed", "worker f0")
        self.assertIn("the agent is now running on account 1, but its Firstmate record still names account 2", line)
        self.assertIn("set account=ordinary, herdr_session=test-session, herdr_workspace_id=w1, "
                      f"herdr_tab_id=w1:t1, herdr_pane_id={agent.pane} in {meta}", line)
        self.assertNotIn("moved:", result.stdout)
        self.assertEqual(meta.read_bytes(), original)
        [backup] = meta.parent.glob("f0.meta.pre-account-swap-*")
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(self.box.session_record(self.box.accounts[1], new_pid)["sessionId"], agent.sid)
        self.assertEqual(self.box.pane_claude(agent.pane), new_pid)

    def test_a_failed_resume_returns_the_agent_to_its_own_account(self) -> None:
        agent = self.worker(self.box.domain, "f1")
        (self.box.state / "fail-resume-in").write_text(str(self.box.accounts[1]))
        meta = self.box.domain / "state/f1.meta"
        original_record = meta.read_bytes()

        result = self.box.run("worker", "f1", "--to", "1", "--go")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("worker f1 (dom): move failed after the stop signal (the resume exited at once)\n",
                      result.stdout)
        self.assertIn("worker f1 (dom): resuming it on its own account 2\n", result.stdout)
        new_pid = self.assert_resumed_on(agent, 2)
        self.assertIn(f"restored worker f1 (dom): not moved: the resume exited at once; resumed on its own "
                      f"account 2 as pid {new_pid} with conversation {agent.sid}", result.stdout)
        self.assertIn("did not complete, so it resumed on its own account 2", self.box.claude_launch(new_pid)["argv"][-1])
        self.assertEqual(meta.read_bytes(), original_record, "the record is not touched")
        [backup] = meta.parent.glob("f1.meta.pre-account-swap-*")
        self.assertEqual(backup.read_bytes(), original_record)
        self.assertTrue(agent.transcript.is_file())

    def test_an_agent_that_outlives_the_stop_is_reported_stopped_never_running(self) -> None:
        # Regression: when the stop wait ran out the agent was reported as
        # "still running as it was", though it had been sent the stop signal.
        agent = self.worker(self.box.domain, "k1", environment={"STUB_IGNORE_TERM": "1"})
        meta = self.box.domain / "state/k1.meta"
        original_record = meta.read_bytes()

        result = self.box.run("worker", "k1", "--to", "1", "--go", constants={"STOP_SECONDS": 1, "SHELL_SECONDS": 1})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        line = self.outcome_line(result.stdout, "failed", "worker k1")
        self.assertIn(f"pid {agent.pid} was still running 1 s after the stop signal; it was sent the stop signal "
                      "and has not exited, so nothing was resumed and its record is unchanged", line)
        self.assertNotIn("still running as it was", result.stdout)
        self.assertNotIn("resuming it", result.stdout)
        self.assertNotIn(("pane", "run"), self.box.herdr_commands(), "nothing is typed over a live agent")
        self.assertEqual(meta.read_bytes(), original_record)
        self.assertTrue(alive(agent.pid))

        # The printed instruction works: once the agent has exited, the back
        # command resumes it on its own account with the same conversation.
        command = re.search(r"type in pane " + re.escape(agent.pane) + r": (sh \S+)$", line).group(1)
        self.assertTrue(shlex.split(command)[1].endswith("worker-k1.back-to-account-2.sh"))
        os.kill(agent.pid, signal.SIGKILL)
        wait_for(lambda: not alive(agent.pid), what="the stand-in to exit")
        self.box.type_into(agent.pane, command)
        new_pid = self.assert_resumed_on(agent, 2)
        self.assertIn("did not complete, so it resumed on its own account 2", self.box.claude_launch(new_pid)["argv"][-1])

    def test_an_agent_that_exits_late_after_the_stop_is_resumed_on_its_own_account(self) -> None:
        agent = self.worker(self.box.domain, "d1", environment={"STUB_TERM_DELAY": "3"})
        meta = self.box.domain / "state/d1.meta"
        original_record = meta.read_bytes()

        result = self.box.run("worker", "d1", "--to", "1", "--go", constants={"STOP_SECONDS": 1})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        new_pid = self.assert_resumed_on(agent, 2)
        self.assertIn(f"restored worker d1 (dom): not moved: pid {agent.pid} was still running 1 s after the stop "
                      f"signal; resumed on its own account 2 as pid {new_pid} with conversation {agent.sid}",
                      result.stdout)
        self.assertNotIn("still running as it was", result.stdout)
        self.assertFalse(alive(agent.pid))
        self.assertEqual(meta.read_bytes(), original_record)

    def test_a_file_the_source_drops_at_the_stop_is_not_left_in_the_target(self) -> None:
        # Regression: the final check only walked the source, so a file copied
        # early that the source then dropped stayed in the target, and the copy
        # was still reported exact.
        sid = str(uuid.uuid4())
        agent = self.worker(self.box.domain, "x1", environment={
            "STUB_SESSION_ID": sid, "STUB_DELETE_ON_TERM": "subagents/agent-1.jsonl"})
        slug = agent.transcript.parent.name
        side = self.box.accounts[2] / "projects" / slug / sid
        (side / "subagents").mkdir(parents=True)
        (side / "subagents/agent-1.jsonl").write_text('{"dropped at the stop": true}\n')
        (side / "kept.json").write_text('{"kept": true}\n')

        result = self.box.run("worker", "x1", "--to", "1", "--go")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_resumed_on(agent, 1)
        target = self.box.accounts[1] / "projects" / slug / sid
        self.assertFalse((side / "subagents/agent-1.jsonl").exists(), "the stand-in dropped it")
        self.assertFalse((target / "subagents/agent-1.jsonl").exists(), "the target matches the source")
        self.assertFalse((target / "subagents").exists(), "an emptied folder is not left behind")
        self.assertEqual((target / "kept.json").read_text(), '{"kept": true}\n')
        self.assertIn("final copy verified byte for byte, both ways", result.stdout)

    def test_a_file_written_into_the_target_by_another_process_resumes_on_the_source(self) -> None:
        agent = self.worker(self.box.domain, "y1", environment={"STUB_WRITE_ON_TERM": str(self.box.accounts[1])})
        meta = self.box.domain / "state/y1.meta"
        original_record = meta.read_bytes()
        foreign = self.box.accounts[1] / "projects" / agent.transcript.parent.name / agent.sid / "foreign.txt"

        result = self.box.run("worker", "y1", "--to", "1", "--go")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        new_pid = self.assert_resumed_on(agent, 2)
        self.assertIn(f"restored worker y1 (dom): not moved: the final copy does not match the source "
                      f"({foreign} (not in the source)); resumed on its own account 2 as pid {new_pid}",
                      result.stdout)
        self.assertEqual(foreign.read_text(), "written into the target by another process\n",
                         "a file this run did not write is never removed")
        self.assertEqual(meta.read_bytes(), original_record)

    # Usage.

    def test_usage_errors_exit_two(self) -> None:
        profile = str(self.box.accounts[2])
        for arguments in ([], ["worker", "t1"], ["worker", "t1", "--to", "4"], ["all", "--to", "1"],
                          ["all", "--from", "2", "--to", "2"], ["worker", "../x", "--to", "1"],
                          ["worker", "t1", "--to", "3"], ["worker", "t1", "--to", "1", "--home", "nowhere"],
                          ["worker", "t1", "--to", "acct2"], ["worker", "t1", "--to", "account2"],
                          ["worker", "t1", "--to", "account 2"], ["worker", "t1", "--to", " 2"],
                          ["worker", "t1", "--to", "02"], ["worker", "t1", "--to", profile],
                          ["all", "--from", "acct2", "--to", "1"],
                          ["worker", "t1", "--to", "1", "--wait-idle", "5"],
                          ["worker", "t1", "--to", "1", "--even-if-busy"],
                          ["worker", "t1", "--to", "1", "--note", "hello"]):
            with self.subTest(arguments=arguments):
                result = self.box.run(*arguments)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("no folder on this machine", self.box.run("worker", "t1", "--to", "3").stderr)
        self.assertIn("not an account: 'acct2' (use 1, 2 or 3)", self.box.run("worker", "t1", "--to", "acct2").stderr)

    def test_nothing_matched_exits_three(self) -> None:
        self.assertEqual(self.box.run("worker", "missing", "--to", "1").returncode, 3)
        self.assertEqual(self.box.run("lead", "missing", "--to", "1").returncode, 3)
        empty = self.box.run("all", "--from", "2", "--to", "1")
        self.assertEqual(empty.returncode, 3, empty.stdout + empty.stderr)
        self.assertIn("nothing recorded runs on account 2", empty.stdout)

    def test_help_names_the_rules_and_only_the_kept_options(self) -> None:
        result = self.box.run("--help")
        self.assertEqual(result.returncode, 0)
        text = " ".join(result.stdout.split())
        for phrase in ("Without --go nothing is stopped, copied or written", "captain's word",
                       "A busy agent is moved at its next pause; one still busy after 240 s is left running",
                       "It never changes which account new workers start on",
                       "never touches credentials, the Keychain, login items or any account's settings"):
            self.assertIn(phrase, text)
        for scope in ("worker", "lead", "all"):
            options = self.box.run(scope, "--help")
            self.assertEqual(options.returncode, 0)
            for removed in ("--note", "--wait-idle", "--even-if-busy"):
                self.assertNotIn(removed, options.stdout)

    def test_the_tool_runs_as_a_command(self) -> None:
        environment = {**self.box.environment(),
                       "PATH": f"{Path(sys.executable).parent}:{self.box.stubs}:/usr/bin:/bin"}
        result = subprocess.run([str(TOOL), "--help"], text=True, capture_output=True, env=environment,
                                timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("usage: account-swap "), result.stdout)


class SkillTest(unittest.TestCase):
    """The skill's frontmatter is read by the agent harness, so it is checked as data."""

    def frontmatter(self) -> dict[str, str]:
        text = SKILL.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"), "the skill opens with frontmatter")
        block, separator, _ = text[4:].partition("\n---\n")
        self.assertTrue(separator, "the frontmatter is closed")
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, colon, value = line.partition(":")
            self.assertTrue(colon and key.strip() and value.strip(), f"not a key: value line: {line!r}")
            self.assertNotIn(key.strip(), fields, f"{key} is set twice")
            fields[key.strip()] = value.strip()
        return fields

    def test_frontmatter_names_the_skill_and_describes_it(self) -> None:
        fields = self.frontmatter()
        self.assertEqual(set(fields), {"name", "description"})
        self.assertEqual(fields["name"], "account-swap")
        self.assertEqual(fields["name"], SKILL.parent.name, "the name matches the skill's folder")
        self.assertRegex(fields["name"], r"^[a-z0-9]+(-[a-z0-9]+)*$")
        self.assertLessEqual(len(fields["name"]), 64)
        self.assertTrue(1 <= len(fields["description"]) <= 1024)
        self.assertNotRegex(fields["description"], r"[<>]")


if __name__ == "__main__":
    unittest.main()
