#!/usr/bin/env python3
"""Post and maintain a single live progress comment on the source pull request.

A run of this action can take an hour: Lean setup, a long coding-agent session,
then independent verification. Until now the pull request stayed silent for all
of it and only learned the outcome at the end, so a reviewer could not tell a
running job from one that was never triggered.

This posts one comment per run as soon as the work starts and then edits that
same comment in place. Editing rather than appending keeps the pull request
readable: the notification fires once, and the comment afterwards always shows
current state instead of a stack of stale ones.

While the coding agent is running, the agent's own `TodoWrite` list is mirrored
into the comment. Claude Code writes its session transcript incrementally to
`$HOME/.claude/projects/**/*.jsonl`, and the sandbox's `HOME` lives under
`RUNNER_TEMP`, which the runner can read -- so a watcher outside the sandbox can
follow the task list live. The base action's own execution log is written only
after the process exits, so it is used as the end-of-run fallback, not the live
source.

Everything here is best-effort reporting. No failure in this script may fail a
run: `main` swallows exceptions and always exits 0.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

WORK = Path(".ai-lean-generate")
STATE = WORK / "progress.json"
STOP = WORK / "progress-watch.stop"

# Hidden, machine-readable copy of the state, embedded in every rendered body.
STATE_OPEN = "<!-- ai-lean-generate-state:"
STATE_CLOSE = "-->"

# Ordered; the table renders in this order regardless of update order.
PHASES: list[tuple[str, str]] = [
    ("prepare", "Prepare task"),
    ("agent", "Coding agent"),
    ("verify", "Independent verification"),
    ("publish", "Publish generated files"),
]

STATUS_ICONS = {
    "pending": "⏳ queued",
    "running": "🔄 running",
    "ok": "✅ done",
    "fail": "❌ failed",
    "skip": "➖ skipped",
}

TODO_ICONS = {
    "completed": "x",
    "in_progress": " ",
    "pending": " ",
}


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def marker() -> str:
    run_id = env("GITHUB_RUN_ID", "manual")
    attempt = env("GITHUB_RUN_ATTEMPT", "1")
    return f"<!-- ai-lean-generate:progress:{run_id}:{attempt} -->"


# --------------------------------------------------------------------------
# GitHub REST
# --------------------------------------------------------------------------


def api(method: str, path: str, payload: dict | None = None) -> object:
    token = env("AI_LEAN_PROGRESS_TOKEN")
    if not token:
        raise RuntimeError("no progress token")
    base = env("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    request = urllib.request.Request(
        f"{base}{path}",
        method=method,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "ai-lean-generate",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body else None


def repo() -> str:
    return env("GITHUB_REPOSITORY")


def find_comment(with_body: bool = False):
    """Locate this run's comment by its hidden marker.

    The state file is per-job, so a later job in the same workflow run -- the
    publish job, say -- has no comment id to reuse. The marker carries the run
    id, so re-discovering the comment from the API works from anywhere.
    """
    pr = env("AI_LEAN_PR_NUMBER")
    if not pr:
        return None
    needle = marker()
    page = 1
    while page <= 10:
        comments = api(
            "GET", f"/repos/{repo()}/issues/{pr}/comments?per_page=100&page={page}"
        )
        if not isinstance(comments, list) or not comments:
            break
        for comment in comments:
            if isinstance(comment, dict) and needle in (comment.get("body") or ""):
                if with_body:
                    return int(comment["id"]), comment.get("body") or ""
                return int(comment["id"])
        if len(comments) < 100:
            break
        page += 1
    return (None, "") if with_body else None


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


def blank_state() -> dict:
    return {
        "comment_id": None,
        "phases": {key: "pending" for key, _ in PHASES},
        "details": {},
        "todos": [],
        "context": "",
        "usage": "",
        "last_body": "",
    }


def load_state() -> dict:
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return blank_state()
    merged = blank_state()
    if isinstance(state, dict):
        merged.update({k: v for k, v in state.items() if k in merged})
    return merged


def parse_body(body: str) -> dict:
    """Recover state from the machine-readable block carried by the comment.

    The publish and failure-reporting jobs run on their own runners and have no
    copy of the state file, so without this they would rerender from a blank
    state and reset every phase the generate job had already reported. The
    comment is the durable record, so each render embeds its own state in a
    hidden block and later jobs read it back.
    """
    state = blank_state()
    start = body.find(STATE_OPEN)
    if start < 0:
        return state
    end = body.find(STATE_CLOSE, start)
    if end < 0:
        return state
    try:
        stored = json.loads(body[start + len(STATE_OPEN) : end])
    except json.JSONDecodeError:
        return state
    if isinstance(stored, dict):
        state.update({k: v for k, v in stored.items() if k in state})
    return state


def save_state(state: dict) -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# Agent task list
# --------------------------------------------------------------------------


def todos_from_objects(objects) -> list[dict] | None:
    """Return the last `TodoWrite` argument list found, newest wins."""
    found: list[dict] | None = None
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        message = obj.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            content = obj.get("content") if isinstance(obj.get("content"), list) else []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if block.get("name") != "TodoWrite":
                continue
            todos = (block.get("input") or {}).get("todos")
            if isinstance(todos, list):
                found = [item for item in todos if isinstance(item, dict)]
    return found


def transcript_files() -> list[Path]:
    homes = [
        env("AI_LEAN_SANDBOX_HOME"),
        str(Path(env("RUNNER_TEMP", "/tmp")) / "ai-lean-claude-home"),
        os.path.expanduser("~"),
    ]
    files: list[Path] = []
    for home in homes:
        if not home:
            continue
        projects = Path(home) / ".claude" / "projects"
        if not projects.is_dir():
            continue
        files.extend(p for p in projects.rglob("*.jsonl") if p.is_file())
    return sorted(files, key=lambda p: p.stat().st_mtime)


def read_jsonl(path: Path) -> list[dict]:
    objects: list[dict] = []
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    objects.append(json.loads(line))
                except json.JSONDecodeError:
                    # A live transcript can be read mid-write; a torn final
                    # line is expected and simply has no todos in it yet.
                    continue
    except OSError:
        return []
    return objects


def current_todos() -> list[dict]:
    for path in reversed(transcript_files()):
        todos = todos_from_objects(read_jsonl(path))
        if todos:
            return todos
    execution = WORK / "claude-execution.json"
    if execution.is_file():
        try:
            payload = json.loads(execution.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, json.JSONDecodeError):
            return []
        if isinstance(payload, dict):
            payload = payload.get("messages") or payload.get("events") or []
        if isinstance(payload, list):
            return todos_from_objects(payload) or []
    return []


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def run_url() -> str:
    server = env("GITHUB_SERVER_URL", "https://github.com")
    return f"{server}/{repo()}/actions/runs/{env('GITHUB_RUN_ID')}"


def headline(state: dict) -> str:
    phases = state["phases"]
    if phases.get("publish") == "ok":
        return "✅ **Finished.** Generated Lean files were published."
    if "fail" in phases.values():
        return "❌ **Failed.** See the phase table and the run log."
    if all(status in ("ok", "skip") for status in phases.values()):
        return "✅ **Finished.**"
    return "🔄 **Working on this pull request.** This comment updates as the run progresses."


def usage_line() -> str:
    try:
        usage = json.loads((WORK / "token-usage.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(usage, dict):
        return ""
    cost = usage.get("cost_usd")
    cost_text = f"${cost:.2f}" if isinstance(cost, (int, float)) else "unknown"
    return (
        f"\nAgent usage: {usage.get('total', 0):,} tokens over "
        f"{usage.get('turns', 0)} turns, cost {cost_text}.\n"
    )


def context_line() -> str:
    parts = []
    provider = env("AI_LEAN_PROGRESS_PROVIDER")
    model = env("AI_LEAN_PROGRESS_MODEL")
    if provider:
        parts.append(f"Provider `{provider}`" + (f" (`{model}`)" if model else ""))
    head = env("AI_LEAN_PROGRESS_HEAD_SHA")
    if head:
        parts.append(f"head `{head[:7]}`")
    turns = env("AI_LEAN_PROGRESS_MAX_TURNS")
    if turns and turns != "0":
        parts.append(f"turn budget {turns}")
    return ", ".join(parts) + "." if parts else ""


def render(state: dict) -> str:
    lines = [marker(), "", "### 🤖 AI Lean Generate", "", headline(state), ""]

    # Later jobs update the same comment without the generate job's environment,
    # so the run context is kept in the state rather than recomputed each time.
    context = context_line() or state.get("context", "")
    state["context"] = context
    if context:
        lines += [context, ""]

    lines += ["| Phase | Status | Detail |", "| --- | --- | --- |"]
    for key, label in PHASES:
        status = state["phases"].get(key, "pending")
        detail = str(state["details"].get(key, "")).replace("|", "\\|")
        lines.append(f"| {label} | {STATUS_ICONS.get(status, status)} | {detail} |")

    todos = state.get("todos") or []
    if todos:
        done = sum(1 for todo in todos if todo.get("status") == "completed")
        lines += ["", f"**Agent task list** ({done}/{len(todos)} complete)", ""]
        for todo in todos:
            status = str(todo.get("status", "pending"))
            text = str(todo.get("content") or todo.get("activeForm") or "").strip()
            if not text:
                continue
            box = TODO_ICONS.get(status, " ")
            suffix = " *(in progress)*" if status == "in_progress" else ""
            lines.append(f"- [{box}] {text}{suffix}")

    usage = usage_line() or state.get("usage", "")
    state["usage"] = usage
    lines += [usage, f"[Workflow run]({run_url()})"]

    body = "\n".join(lines).strip() + "\n"
    carried = {key: state[key] for key in ("phases", "details", "todos", "context", "usage")}
    # `>` is escaped so no detail text can close the HTML comment early; the
    # JSON decoder reads > back as the same character.
    encoded = json.dumps(carried).replace(">", "\\u003e")
    return f"{body}\n{STATE_OPEN} {encoded} {STATE_CLOSE}\n"


# --------------------------------------------------------------------------
# Publishing
# --------------------------------------------------------------------------


def publish(state: dict) -> None:
    pr = env("AI_LEAN_PR_NUMBER")
    if not pr or not env("AI_LEAN_PROGRESS_TOKEN"):
        return
    body = render(state)
    if body == state.get("last_body"):
        return
    comment_id = state.get("comment_id") or env("AI_LEAN_PROGRESS_COMMENT_ID") or None
    if comment_id:
        comment_id = int(comment_id)
    else:
        comment_id = find_comment()
    if comment_id:
        try:
            api(
                "PATCH",
                f"/repos/{repo()}/issues/comments/{comment_id}",
                {"body": body},
            )
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            comment_id = None
    if not comment_id:
        created = api("POST", f"/repos/{repo()}/issues/{pr}/comments", {"body": body})
        comment_id = int(created["id"]) if isinstance(created, dict) else None
    state["comment_id"] = comment_id
    state["last_body"] = body
    save_state(state)


def set_output(name: str, value: str) -> None:
    path = env("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def command_set(args: argparse.Namespace) -> None:
    state = load_state()
    if not STATE.is_file() and env("AI_LEAN_PROGRESS_TOKEN"):
        comment_id, body = find_comment(with_body=True)
        if comment_id:
            state = parse_body(body)
            state["comment_id"] = comment_id
    if args.phase:
        state["phases"][args.phase] = args.status
        if args.detail:
            state["details"][args.phase] = args.detail
    if args.refresh_todos:
        todos = current_todos()
        if todos:
            state["todos"] = todos
    publish(state)
    save_state(state)
    if state.get("comment_id"):
        set_output("progress-comment-id", str(state["comment_id"]))


def command_watch(args: argparse.Namespace) -> None:
    """Mirror the agent's task list into the comment while the agent runs."""
    deadline = time.monotonic() + args.max_seconds
    while time.monotonic() < deadline and not STOP.exists():
        time.sleep(args.interval)
        try:
            state = load_state()
            todos = current_todos()
            if todos and todos != state.get("todos"):
                state["todos"] = todos
                publish(state)
                save_state(state)
        except Exception as error:  # noqa: BLE001 - reporting must never fail a run
            print(f"progress watch: {error}", file=sys.stderr)


def command_stop(_: argparse.Namespace) -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    STOP.write_text("stop\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    setter = sub.add_parser("set", help="set a phase status and republish")
    setter.add_argument("--phase", choices=[key for key, _ in PHASES], default="")
    setter.add_argument("--status", choices=sorted(STATUS_ICONS), default="running")
    setter.add_argument("--detail", default="")
    setter.add_argument("--refresh-todos", action="store_true")
    setter.set_defaults(func=command_set)

    watcher = sub.add_parser("watch", help="mirror the agent task list live")
    watcher.add_argument("--interval", type=float, default=20.0)
    watcher.add_argument("--max-seconds", type=float, default=6 * 3600)
    watcher.set_defaults(func=command_watch)

    stopper = sub.add_parser("stop", help="ask a running watcher to exit")
    stopper.set_defaults(func=command_stop)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except Exception as error:  # noqa: BLE001 - reporting must never fail a run
        print(f"::warning::progress comment update failed: {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
