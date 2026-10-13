"""Thin web front for the test-generation skill.

This file does NOT implement the workflow. It starts the `claude` CLI already
installed on this machine with `/<skill> <inputs>` and relays its event stream to
the browser. The skill (SKILL.md + your scripts) decides every step.

Each run is ONE live Claude session (stream-json in and out). Messages the tester
sends go into that running session, like typing in the terminal: mid-turn they are
picked up at the next tool result, after a turn they start the next turn. The session
ends when the tester clicks Finish, after TESTGEN_IDLE_MIN minutes without a message,
or when the tab is closed while Claude is idle.

Drop this folder into your test repo and run:  python3 testgen-ui/app.py
Optional env: TESTGEN_REPO (default: the git repo this folder sits in),
TESTGEN_SKILL (default: the only skill in .claude/skills), TESTGEN_MAX_TURNS,
TESTGEN_IDLE_MIN (default 15), PORT. Start it from the shell/venv your skill's
scripts need (Playwright etc.).

Every run gets its own folder, testgen-ui/runs/<run-id>/ (git-ignored):
  input.json     what was asked: url, env, brief, prompt, exact claude command
  screenshot.*   the annotated screenshot that was uploaded
  events.ndjson  every event: Claude's stream plus the tester's messages, one JSON object per line
  stderr.log     anything claude printed to stderr
  result.json    latest outcome, cost, turns, changed files + compile status, Claude session id
"""
import base64
import json
import os
import pathlib
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
_top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=HERE, capture_output=True, text=True).stdout.strip()
REPO = pathlib.Path(os.environ.get("TESTGEN_REPO") or _top or HERE).resolve()
_skills = sorted(p.parent.name for p in (REPO / ".claude" / "skills").glob("*/SKILL.md"))
SKILL = os.environ.get("TESTGEN_SKILL") or (_skills[0] if len(_skills) == 1 else "")
if not SKILL:
    sys.exit(f"Set TESTGEN_SKILL to one of: {', '.join(_skills) or '(no skills found in ' + str(REPO / '.claude/skills') + ')'}")
MAX_TURNS = os.environ.get("TESTGEN_MAX_TURNS", "60")
IDLE_S = float(os.environ.get("TESTGEN_IDLE_MIN", "15")) * 60
PORT = int(os.environ.get("PORT", "8765"))
INDEX = HERE / "index.html"
RUNS = HERE / "runs"
RUNS.mkdir(exist_ok=True)
(RUNS / ".gitignore").write_text("*\n")  # run artefacts never show up as changed files or get committed

run_lock = threading.Lock()  # ponytail: one session at a time (shared repo); per-run worktree/container if testers share a server
LIVE = {}  # run_id -> Session, while its claude process is alive


def dirty_files():
    """{path: (mtime, "new"|"modified")} for files git sees as changed/untracked; {} if not a git repo."""
    r = subprocess.run(["git", "status", "--porcelain", "-uall"], cwd=REPO, capture_output=True, text=True)
    out = {}
    for line in r.stdout.splitlines() if r.returncode == 0 else []:
        p = REPO / line[3:].split(" -> ")[-1].strip('"')
        out[str(p)] = (p.stat().st_mtime if p.exists() else None, "new" if line[:2] in ("??", "A ") else "modified")
    return out


def compile_check(path):
    """None if not Python, "ok" if it compiles, else the SyntaxError. Writes no .pyc into the repo."""
    if not path.endswith(".py"):
        return None
    try:
        compile(pathlib.Path(path).read_text(encoding="utf-8"), path, "exec")
        return "ok"
    except SyntaxError as e:
        return f"line {e.lineno}: {e.msg}"


def user_message(text):
    """One stream-json input line: a user message for the running session."""
    return json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}) + "\n"


class Session:
    """One run: a live claude process, its log, and the browser stream it reports to."""

    def __init__(self, run_dir, wfile):
        self.dir, self.wfile = run_dir, wfile
        self.lock = threading.Lock()  # the stream thread and /message both write events
        self.browser_open, self.busy, self.idle_timer, self.ended = True, True, None, None
        self.events = open(run_dir / "events.ndjson", "a")
        self.proc = None

    def send(self, obj_or_line):
        """Log the event to the run, then try the browser. A closed tab doesn't stop the run."""
        line = obj_or_line if isinstance(obj_or_line, str) else json.dumps(obj_or_line)
        with self.lock:
            self.events.write(line + "\n")
            self.events.flush()
            if self.browser_open:
                try:
                    self.wfile.write((line + "\n").encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ValueError):
                    self.browser_open = False

    def say(self, text):
        """Put a message into the running session. False if it has already ended."""
        if self.ended or self.proc.poll() is not None:
            return False
        if self.idle_timer:
            self.idle_timer.cancel()
        self.busy = True
        self.send({"type": "testgen_message", "text": text})
        try:
            self.proc.stdin.write(user_message(f"Message from the tester (via the Test Generator UI): {text}"))
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            return False
        return True

    def finish(self, reason):
        """No more messages: claude finishes what it's doing, then exits."""
        if self.ended:
            return
        self.ended = reason
        if self.idle_timer:
            self.idle_timer.cancel()
        try:
            self.proc.stdin.close()
        except (BrokenPipeError, ValueError):
            pass


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/":
            return self.send_error(404)
        body = INDEX.read_text().replace("{{SKILL}}", SKILL).replace("{{REPO}}", str(REPO)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        except Exception:
            return self.send_error(400, "Request body must be JSON")
        if self.path == "/generate":
            try:
                url, env, brief = req["url"].strip(), req["env"].strip(), req["brief"].strip()
                shot_name = os.path.basename(req["screenshot_name"]) or "screenshot.png"
                shot_bytes = base64.b64decode(req["screenshot"].split(",", 1)[-1])
                assert url and env and brief and shot_bytes
            except Exception:
                return self.send_error(400, "URL, environment, brief and screenshot are all required")
            if not run_lock.acquire(blocking=False):
                return self.send_error(409, "Another test session is still open, wait for it to finish")
            try:
                self.run(url, env, brief, shot_name, shot_bytes)
            finally:
                run_lock.release()
        elif self.path in ("/message", "/finish"):
            session = LIVE.get(str(req.get("run_id", "")))
            text = str(req.get("text", "")).strip()
            if not session:
                return self.send_error(409, "This session has ended")
            if self.path == "/message":
                if not text:
                    return self.send_error(400, "Empty message")
                if not session.say(text):
                    return self.send_error(409, "This session has ended")
            else:
                session.finish("finished by tester")
            self.send_response(204)
            self.end_headers()
        else:
            self.send_error(404)

    def run(self, url, env, brief, shot_name, shot_bytes):
        run_id = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        run_dir = RUNS / run_id
        run_dir.mkdir()
        shot = run_dir / ("screenshot" + (pathlib.Path(shot_name).suffix or ".png"))
        shot.write_bytes(shot_bytes)
        prompt = (
            f"/{SKILL} URL: {url}\nEnvironment: {env}\n"
            f"Annotated screenshot: {shot}\nTest brief: {brief}"
        )
        cmd = [
            "claude", "-p",
            "--input-format", "stream-json",   # session stays open; each stdin line is a user message
            "--output-format", "stream-json", "--verbose",
            "--max-turns", MAX_TURNS,
            "--permission-mode", "acceptEdits",
            "--allowedTools", "Skill", "Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep",
            "--add-dir", str(run_dir),  # lets Claude read the screenshot even when TESTGEN_REPO is elsewhere
        ]
        (run_dir / "input.json").write_text(json.dumps({
            "run_id": run_id, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "url": url, "env": env,
            "brief": brief, "screenshot": shot.name, "original_screenshot_name": shot_name,
            "skill": SKILL, "repo": str(REPO), "prompt": prompt, "cmd": cmd,
        }, indent=2))
        before = dirty_files()
        started = time.time()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        s = Session(run_dir, self.wfile)
        written, final, session_id, cost_total, turns_total, files = set(), {}, None, 0.0, 0, []
        with s.events, open(run_dir / "stderr.log", "w") as err:
            s.send({"type": "testgen_run", "id": run_id, "dir": str(run_dir)})
            s.proc = subprocess.Popen(cmd, cwd=REPO, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, text=True)
            LIVE[run_id] = s
            s.proc.stdin.write(user_message(prompt))
            s.proc.stdin.flush()
            try:
                for line in s.proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    s.send(line)  # claude's own events, untouched
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("subtype") == "init":
                        session_id = ev.get("session_id") or session_id
                    elif ev.get("type") == "assistant":
                        for b in ev["message"]["content"]:
                            fp = b.get("name") in ("Write", "Edit", "MultiEdit") and (b.get("input") or {}).get("file_path")
                            if fp:
                                written.add(str((REPO / fp).resolve()))
                    elif ev.get("type") == "result":  # a turn ended; the session waits for the tester
                        final, s.busy = ev, False
                        cost_total = ev.get("total_cost_usd") or cost_total  # already a running total for the session
                        turns_total += ev.get("num_turns") or 0               # this one restarts with every message
                        files = changed_files(before, written)
                        s.send({"type": "testgen_files", "repo": str(REPO), "files": files})
                        write_result(run_dir, run_id, session_id, started, s, final, cost_total, turns_total, files)
                        if not s.browser_open:
                            s.finish("tab closed")  # nobody left to answer
                        elif not s.ended:
                            s.idle_timer = threading.Timer(IDLE_S, s.finish, ["idle timeout"])
                            s.idle_timer.daemon = True
                            s.idle_timer.start()
                s.proc.wait()
            finally:
                LIVE.pop(run_id, None)
                s.finish(s.ended or "claude exited")
                if s.proc.poll() is None:  # only on a crash in this loop
                    s.proc.terminate()
                    s.proc.wait()
            err.flush()
            files = changed_files(before, written)
            if s.proc.returncode:
                s.send({"type": "testgen_error", "message": (run_dir / "stderr.log").read_text()[-2000:]})
            s.send({"type": "testgen_end", "reason": s.ended, "files": files})
        write_result(run_dir, run_id, session_id, started, s, final, cost_total, turns_total, files)


def changed_files(before, written):
    after = dirty_files()
    changed = {p: st for p, (m, st) in after.items() if p not in before or before[p][0] != m}
    for p in written:  # non-git repos, or files git ignores
        if p.startswith(str(REPO)) and os.path.exists(p):
            changed.setdefault(p, "changed")
    return [{"path": p, "status": st, "compile": compile_check(p)}
            for p, st in sorted(changed.items()) if not p.startswith(str(RUNS))]


def write_result(run_dir, run_id, session_id, started, s, final, cost_total, turns_total, files):
    (run_dir / "result.json").write_text(json.dumps({
        "run_id": run_id, "session_id": session_id, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "session": "ended: " + s.ended if s.ended else "open",
        "duration_s": round(time.time() - started, 1), "exit_code": s.proc.poll(),
        "outcome": final.get("subtype", "no result"), "turns_total": turns_total,
        "cost_usd_total": round(cost_total, 4), "claude_summary": final.get("result"),
        "files": files, "browser_stayed_open": s.browser_open,
    }, indent=2))


if __name__ == "__main__":
    if not (REPO / ".claude" / "skills" / SKILL).exists() and not (pathlib.Path.home() / ".claude" / "skills" / SKILL).exists():
        print(f"warning: skill '{SKILL}' not found in {REPO}/.claude/skills or ~/.claude/skills")
    print(f"Test generator on http://localhost:{PORT}  (repo: {REPO}, skill: /{SKILL}, runs: {RUNS})")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
