# Test Generator UI — spec

A web page in front of an existing Claude Code skill that writes UI automation tests.
A tester fills in four inputs, clicks **Generate test**, watches Claude work live, can message Claude at any time (mid-run or after, in the same session, like typing in the terminal), and gets the changed files to review.

This document records how the tool works, **why** it is built this way, and how to improve it without breaking the parts that make it work.
Read all of it before changing code.

---

## 0. Working agreement (for Claude or anyone changing this)

1. **The skill is the product. The app is a window onto it.** `app.py` passes inputs in and passes events out. It must never decide which step runs next.
2. **One live Claude session per run, start to finish, including the tester's messages.** Do not split the run into separate LLM calls per stage (vision, then codegen, and so on). Losing shared context between stages is what made earlier attempts fail (see §1).
3. **Do not write an `orchestrator.py`, and do not port SKILL.md into Python.** If behaviour needs to change, change SKILL.md or the skill's scripts.
4. **Keep it small.** Python standard library only, a single HTML file, no build step, no framework, no database. Add a dependency only when a concrete need appears that the standard library cannot meet, and write down why.
5. **Make the smallest change that solves the problem**, then check it against §12. If a change touches the contract in §4 or §6, update this spec in the same change.

---

## 1. Background and decisions

### The problem this solves
The skill already gives good results from the terminal. It:
1. checks whether existing automation already covers the flow;
2. generates auth for the chosen environment with a Python script;
3. runs `inspect_dom.py` against the URL and writes `element_dump.json`;
4. writes the test from the dump, the brief and the screenshot.

Management wanted the same thing behind a UI that testers could use.

### What failed before
Every UI tool tried first produced an `orchestrator.py` that ran the four steps as separate scripts and called the LLM only at certain points, such as reading the screenshot or generating code.
- **Shared context was lost.** The model never saw the whole job, so results were poor.
- **The skill was no longer really in use.**
- **A small open-source model (about 3.5B parameters) made it worse.** In multi-step tool use it hallucinated and looped, which wasted tokens. Models that size can't reliably manage a multi-step job with vision and tool calls.

### Decisions

| Decision | Why | Alternatives considered |
|---|---|---|
| Run **headless Claude Code** (`claude -p --input-format stream-json --output-format stream-json`) as one live subprocess per run | It is literally the terminal setup: same binary, login, skills, model and agent loop. It streams every event as JSON lines. | **Claude Agent SDK**: the same loop as a library. A valid future option (§13), but it adds a dependency and its own version to pin. **Anthropic Skills API**: its code-execution container has no network access and no package installs, so the auth script and `inspect_dom.py` can't reach test environments. **Managed Agents**: in beta; would need a self-hosted sandbox. |
| Invoke the skill as **`/<skill-name> <inputs>`** | Claude Code expands the slash command into the skill's instructions, exactly as in the terminal. | Pasting SKILL.md into a system prompt, which loses frontmatter, base directory and skill semantics. |
| **Python stdlib server, one HTML file** | Testers run it with only Python and Claude Code installed. Nothing to install or build. | FastAPI, Streamlit or Gradio: more dependencies, no extra capability needed. |
| **Infer stage labels** from tool calls in the browser | No change to the skill was needed to show progress. | Stage markers printed by the skill: more exact, and the recommended upgrade (§13.1). |
| **Fixed compile check** after the run | Confirms the generated Python is valid no matter what the model did. Uses `compile()`, so it writes no `.pyc`. | Trusting the skill to check. |
| **Tester messages go into the live session** (written to the process's stdin as stream-json user messages) | Verified to behave like the terminal: a message sent mid-turn is picked up at the next tool result in the same turn, and a message sent after a turn starts the next turn. Full context is kept either way. | `--resume <session-id>` with a new process per reply. It works, but only between turns, not mid-run. |
| **One session at a time** | Sessions write into one shared repo working tree. A session holds the lock until it ends (Finish, idle timeout, or tab closed while Claude is idle). | A git worktree per run (§13.6). |
| **Each run stored in `runs/<run-id>/`** | Every run can be inspected afterwards. Git-ignored, so it never appears as a changed file. | Temp folders: removed, because they left files behind and couldn't be inspected later. |

---

## 2. Architecture

```
 Browser (index.html)                       app.py (stdlib HTTP, 127.0.0.1:8765)                claude CLI (headless)
 ───────────────────                        ─────────────────────────────────────                ─────────────────────
 form: url, env, screenshot, brief ──POST /generate (JSON)──▶ validate → lock → make runs/<id>/
                                                              save screenshot + input.json
                                                              git status snapshot (before)
                                                              spawn, write first message ──stdin──▶ /<skill> URL:… Env:… Screenshot:… Brief:…
                                                                                                  (runs the skill: coverage → auth script →
                                                                                                   inspect_dom.py → write test)
 live log + stage bar ◀──NDJSON stream── relay each line + append to events.ndjson ◀──stdout── stream-json events
                                            on each `result` (turn ended): git status → changed files,
                                            compile() each changed .py, write result.json, start idle timer
 result panel        ◀── testgen_files ──
 message bar ──POST /message──▶ log testgen_message, write to stdin ──stdin──▶ picked up mid-turn, or starts the next turn
 Finish      ──POST /finish───▶ close stdin ──────────────────────────────────▶ claude finishes current work, exits
 session end         ◀── testgen_end ── final changed files, result.json, release lock
```

- The process runs with `cwd = REPO`, so it sees the repo's `.claude/skills`, `CLAUDE.md` and settings, just like a terminal session started in the repo.
- The HTTP response to `/generate` *is* the event stream for the whole session: one long-lived POST whose body is newline-delimited JSON, open across all turns until the session ends. There is no SSE and no WebSocket. Messages and Finish are short separate POSTs; their effects appear in that same stream.

---

## 3. Files

```
testgen-ui/
  app.py       backend: about 280 lines, stdlib only, Python 3.9+
  index.html   frontend: one file with inline CSS/JS; Google Fonts with system-font fallback
  SPEC.md      this document
  runs/        created on startup; contains .gitignore ("*") and one folder per run
```

---

## 4. Configuration and startup

```bash
python3 testgen-ui/app.py          # open http://localhost:8765
```

| Env var | Default | Meaning |
|---|---|---|
| `TESTGEN_REPO` | the git top level of the folder containing `app.py`, else that folder | Where Claude runs and where changes are detected |
| `TESTGEN_SKILL` | the only skill in `<REPO>/.claude/skills/*/SKILL.md` | Slash-command name. If there are 0 or several skills and none is set, startup exits and lists the choices. |
| `TESTGEN_MAX_TURNS` | `60` | Passed as `--max-turns`; caps a stuck run |
| `TESTGEN_IDLE_MIN` | `15` | Minutes after a turn ends with no tester message before the session closes itself |
| `PORT` | `8765` | Binds to 127.0.0.1 only |

Prerequisites:
- `claude` is on PATH and logged in.
- Start the app from the **same shell or virtualenv** the skill's scripts need (Playwright and so on), because Claude's Bash tool inherits that environment.
- If `ANTHROPIC_API_KEY` is set in that shell, it bills that API key instead of the logged-in account.

Startup side effects: creates `runs/` and writes `runs/.gitignore`. If the skill folder isn't found in the repo or in `~/.claude/skills`, it prints a warning.

---

## 5. HTTP API

### `GET /`
Returns `index.html` with `{{SKILL}}` and `{{REPO}}` substituted. Any other path returns 404.

### `POST /generate`
Request body (JSON):
```json
{
  "url": "https://staging.acme.com/profile",
  "env": "staging",
  "brief": "1. Change Display name (box 1)\n2. Click Save (box 2)\nExpect: …",
  "screenshot": "data:image/png;base64,iVBOR…",
  "screenshot_name": "profile.png"
}
```
- `url`, `env` and `brief` must be non-empty after trimming. `screenshot` must decode to non-empty bytes; a `data:` prefix is optional.
- **400**: missing or invalid fields. Message: `URL, environment, brief and screenshot are all required`.
- **409**: a session is still open. Message: `Another test session is still open, wait for it to finish`.
- Error bodies are Python's default HTML error page; the UI pulls out `<p>Message: …</p>`.

On success: **200**, `Content-Type: application/x-ndjson`, streamed until the **session** ends. Each line is one JSON object. One turn looks like this; later turns repeat rows 2 to n‑1:

| # | `type` | From | Notes |
|---|---|---|---|
| 1 | `testgen_run` | app | `{id, dir}`: run ID and absolute run folder |
| 2… | `system` (`hook_started`, `hook_response`, `init`, …) | claude | `init` carries `session_id`, `model` and `slash_commands`; the UI uses these to detect a missing skill. **Claude re-sends `init` at the start of every turn**, and the UI logs only the first. |
| … | `assistant` | claude | `message.content[]`: `text` blocks, `tool_use` blocks (`{id, name, input}`), and `thinking` blocks (ignored) |
| … | `user` | claude | `message.content[]` with `tool_result` blocks (`{tool_use_id, content, is_error}`) |
| any time | `testgen_message` | app | `{text}`: a tester message, logged the moment it is sent into the session |
| … | `rate_limit_event`, others | claude | Ignored by the UI; still logged |
| end of turn | `result` | claude | `subtype` (`success`, `error_max_turns`, `error_during_execution`, …), `is_error`, `result` (final text). **`num_turns` restarts with every message**, while **`total_cost_usd` is already a running total for the session**. Add up turns; never add up cost. |
| end of turn | `testgen_files` | app | `{repo, files: [{path, status, compile}]}` relative to the state before the *first* turn; see §8 |
| end of session | `testgen_error` | app | Only if claude exited non-zero: `{message}` = last 2000 characters of stderr |
| end of session | `testgen_end` | app | `{reason, files}`. `reason` is one of `finished by tester`, `idle timeout`, `tab closed`, `claude exited` |

Claude's lines are passed through **unchanged**. The app adds only the `testgen_*` event types.

**Closing the tab does not stop work in progress.** Writing to the browser fails quietly and Claude finishes its current turn. Because nobody is left to reply, the session then ends (`tab closed`). Everything is saved to the run folder.

### `POST /message`
`{"run_id": "...", "text": "..."}` → **204**. The text is logged as `testgen_message` and written to Claude's stdin as a user message, prefixed with `Message from the tester (via the Test Generator UI): ` so Claude knows where it came from.
- **Mid-turn:** Claude Code attaches it to the next tool result and acts on it in the same turn.
- **After a turn:** it starts the next turn.

Errors: **400** for an empty message, **409** for an ended or unknown session.

Verified with real Claude:
- A mid-turn "also include the word ZEBRA" was acted on within the same turn. On one of two tries Claude first treated the message cautiously because it arrived next to tool output, so clear, direct wording helps.
- A follow-up after the test was written edited the same test, in the same session.

### `POST /finish`
`{"run_id": "..."}` → **204**. Closes Claude's stdin: Claude finishes what it is doing, then exits, and the stream ends with `testgen_end`.

---

## 6. The Claude invocation (contract with the skill)

### The prompt
```
/<SKILL> URL: <url>
Environment: <env>
Annotated screenshot: <absolute path to runs/<id>/screenshot.<ext>>
Test brief: <brief>
```
**The skill must read these four labelled lines from its arguments (`$ARGUMENTS`).** If the skill expects another format, change the prompt in `app.py` (one f-string), or better, make the skill accept this format. This is the only coupling between app and skill.

### The command
```
claude -p --input-format stream-json      # session stays open; every stdin line is a user message (the first is the prompt)
  --output-format stream-json --verbose     # one JSON event per line (--verbose is required with -p for stream-json)
  --max-turns $TESTGEN_MAX_TURNS            # runaway guard
  --permission-mode acceptEdits             # nobody is there to approve file edits
  --allowedTools Skill Bash Read Write Edit MultiEdit Glob Grep   # pre-approved; anything else gets denied, not prompted
  --add-dir <run folder>                    # lets Claude read the screenshot even if TESTGEN_REPO is elsewhere
cwd = REPO, stdin = pipe (stream-json user messages), stderr → runs/<id>/stderr.log
```
A stdin line looks like this:
```json
{"type":"user","message":{"role":"user","content":[{"type":"text","text":"..."}]}}
```
Closing stdin ends the session after the current work. The session ID from `init` is saved in `result.json`, so a finished conversation can be continued by hand in a terminal with `claude --resume <session_id>`.
- **Do not add `--bare`.** It skips skill discovery, and it is planned to become the default for `-p`. Pin the Claude Code version you deploy with, and re-check that skills still load (the `init.slash_commands` list) when upgrading.

### What headless mode means for the skill
| Terminal behaviour | Headless behaviour | What the skill should do |
|---|---|---|
| Asks a permission question | Pre-approved tools run; others are denied | Use only the allowed tools |
| Asks *you* a clarifying question | The turn ends with the question as its `result` text, and the tester answers in the message bar, in the same session | Asking is fine. Keep questions specific so they're easy to answer. |
| A script calls `input()` / "press Enter" | The script gets EOF and fails | Wait on the browser (selector or URL) instead of stdin |
| A headed browser opens | Still opens on the machine running the app | Fine when testers run it locally; it's the human-in-the-loop step. The skill can stop and ask "reply when you're logged in", and the tester replies in the message bar. |

---

## 7. Run folder: `runs/<run-id>/`

Run ID: `YYYYMMDD-HHMMSS-xxxx` (local time plus 4 random hex characters). The whole `runs/` folder is git-ignored and nothing deletes old runs; clean up by hand.

| File | Written | Contents |
|---|---|---|
| `input.json` | before Claude starts | `run_id, started_at, url, env, brief, screenshot, original_screenshot_name, skill, repo, prompt, cmd` (the exact argv) |
| `screenshot.<ext>` | before Claude starts | The upload, renamed; extension kept, `.png` if missing |
| `events.ndjson` | as events arrive | Every streamed line, including the `testgen_*` events and the tester's messages, so it replays exactly what the browser saw |
| `stderr.log` | during the run | Claude's stderr |
| `result.json` | after every turn and at session end | `run_id, session_id, updated_at, session` (`open` or `ended: <reason>`), `duration_s, exit_code, outcome` (the latest result `subtype`, or `"no result"`), `turns_total, cost_usd_total, claude_summary, files, browser_stayed_open` |

Inspecting a run:
```bash
jq -r 'select(.type=="assistant") | .message.content[] | select(.type=="tool_use") | "\(.name) \(.input.command // .input.file_path // .input.pattern // "")"' runs/<id>/events.ndjson
jq . runs/<id>/result.json
```
`events.ndjson` lines are the input to the page's `handle()` function. Feeding them back in reproduces the UI exactly, which is useful for UI work without spending tokens (§12).

---

## 8. Changed-file detection and compile check

1. **Before** the run: `git status --porcelain -uall` gives `{path: (mtime, "new"|"modified")}` for every file that is already changed or untracked. `??` and `A ` count as new; everything else counts as modified. Renames use the new path.
2. **After** the run: the same snapshot again. A file is reported if it is newly dirty, **or** it was already dirty but its mtime changed. So pre-existing local edits are not blamed on the run unless the run touched them.
3. **Fallback:** any `file_path` from `Write`, `Edit` or `MultiEdit` tool calls that exists inside the repo is added with status `changed`. This covers non-git repos and git-ignored files.
4. Anything under `runs/` is excluded.
5. Each `.py` file gets `compile_check`: `"ok"`, or `"line N: <SyntaxError msg>"`. Other files get `null`.

Limits:
- Files changed by Bash commands (`cp`, scripts) in a **non-git** repo are missed; git repos catch them.
- `compile()` checks syntax only. Imports, fixtures and selectors are verified only by running the test, which is the human verification step.

---

## 9. UI (`index.html`)

### Layout
Dark only. Three columns at 1240px and wider; two columns with the guide moving below up to 860px; a single column under 860px.
1. **Form:** Page URL (`type=url`, required), Environment (required, free text with a datalist of dev, qa and staging), Annotated screenshot (drop, paste or click; image only; required; preview shown), Test brief (required).
   - **Use template** fills in a step-by-step skeleton.
   - Under the brief, four quiet hints turn green when the brief appears to include: *steps*, *expected result*, *box refs*, *test data* (regexes in `HINTS`). They are hints only and don't block submission.
   - **Generate test**; ⌘/Ctrl+Enter also submits.
   - URL and environment are remembered in `localStorage` (per browser, best-effort).
2. **Run panel:** stage bar (Coverage → Auth → DOM inspect *(you)* → Write test → Verify *(you)*), the "Your turn" notice, the live log (with a copy link), the **message bar**, and the result.
   - **Message bar:** a text box, **Send** and **Finish**. Visible from `testgen_run` until `testgen_end`. A sent message appears in the log as `› you: …` once the server has put it into the session (`testgen_message`). Sending also hides the previous result, clears the ✕ and "skipped" marks on stages, and resumes the timer; the next `testgen_files` shows the updated result.
   - While a session is open the Generate button reads "Session open…" and is disabled.
3. **Guide:** How it works, Writing a good brief (with one example), and Results.

Header status: Ready, Running with a live timer, then Ready to verify, No changes, Failed or Skill not found, each with the elapsed time since the session started. "· ended" is added when the session closes.

### Stage inference (`classify`), forward-only
| Tool call | Stage |
|---|---|
| `Bash` with a command matching `/inspect_dom/` | DOM inspect (amber, shows the "Your turn" notice) |
| `Bash` with a command matching `/auth/i` | Auth |
| `Write`/`Edit`/`MultiEdit`, not `element_dump` | Write test |
| `Skill`/`Read`/`Grep`/`Glob` before Auth | Coverage |

- A successful `tool_result` for the `inspect_dom` call marks DOM done and hides the notice.
- Stages never move backwards, and earlier stages are marked done when a later one starts.
- **The labels depend on script names.** If your auth script isn't called `*auth*`, that stage is skipped visually; the run itself is unaffected. §13.1 is the robust fix.

### Outcomes (`showResult`)
| Condition | Shown |
|---|---|
| `init.slash_commands` doesn't include the skill | "Skill /x not found" plus how to fix it; unreached stages greyed |
| No `result` event, `is_error`, or `subtype != success` | "No test generated", the stage that was running gets ✕, a reason (turn limit shows the count), Claude's note opened |
| Success with no changed files | "No changes, probably already covered"; reached stages ✓, the rest "skipped"; Claude's note opened |
| Success with changed files | "New test written", or "Existing test extended" if any **modified** `.py`. Files listed with NEW/MODIFIED, compile result and "Open ↗" (`vscode://file…`). Clicking a path copies it. Non-`.py` files are dimmed. A red "Won't compile" notice if any `.py` fails. Then "Your turn: open it in VS Code or PyCharm and run the test. Something off? Tell Claude in the box above." Claude's note is collapsed. |

The footer of every outcome shows time · turns (added up across messages) · cost (the session total as reported) · **run ID**; clicking the run ID copies the run folder path.

The coverage outcome maps like this:
- **not covered:** NEW test file;
- **partially covered:** MODIFIED existing test;
- **fully covered:** no changes, and Claude's note says where it's covered.

---

## 10. Verified behaviour (as of 2026-10-13, Claude Code 2.1.296, macOS, Python 3.9)

- **Real Claude, end to end, four times, on a dummy skill.** The skill was found, Claude ran the auth script and `inspect_dom.py`, read the screenshot from the run folder, and wrote a test that compiles. About 8 turns, $0.18–0.20, about 25 seconds each.
- **Live session with real Claude:**
  - a follow-up message after the test was written ("add a docstring") edited the same test in the same session: +2 turns, +$0.02, then Finish closed it cleanly;
  - a mid-turn message sent during a running Bash command was picked up within the same turn;
  - `total_cost_usd` is a running session total (0.088, then 0.186), while `num_turns` restarts per message.
- **Stand-in `claude`, through the real server and page:**
  - not covered, partially covered and fully covered;
  - generated code that doesn't compile;
  - turn-limit failure;
  - skill not found;
  - second submit (409);
  - missing fields (400);
  - tab closed mid-run (run completes and `result.json` is written);
  - message mid-turn, message after a turn, Finish, idle timeout, message to an ended session (409), Generate while a session is open (409);
  - phone and laptop widths.
- **`runs/` never appears** in `git status` or in changed files.
- **Skills outside the repo:** `--plugin-dir <dir>`, where `<dir>/.claude-plugin/plugin.json` contains `{"name":"testgen"}` and `<dir>/skills/<folder>` is the skill (a symlink works), exposes `/testgen:<folder>`. The **folder name**, not the frontmatter `name`, decides the command. `--add-dir` does **not** load skills. Not implemented, because the skill lives in the repo.

**Not verified: the real skill and scripts.** First things to check in the target repo:
1. the skill parses the four-line prompt (§6);
2. `inspect_dom.py` and the auth script never read stdin;
3. any questions the skill asks are specific enough to answer in one message;
4. the script names match the stage rules (§9).

---

## 11. Known limitations

- One session at a time per server; the shared working tree is the reason. An open session blocks others until Finish, the idle timeout (`TESTGEN_IDLE_MIN`), or the tab closing.
- Bound to 127.0.0.1 with no authentication, so it is not safe to expose as is. It runs Bash with pre-approved tools.
- Human-in-the-loop steps are notices, plus whatever the tester says in the message bar. Nothing can be typed into a *script's* stdin.
- A mid-turn message is attached to Claude's next tool result. Claude occasionally treats it cautiously because of where it appears, so tell testers to write messages as clear instructions.
- No hard cancel. Finish lets Claude complete its current work first. To stop immediately, use `Ctrl+C` on the server or kill the `claude` process.
- A session that was ended can't be reopened from the UI; use `claude --resume <session_id>` in a terminal.
- Stage labels are guessed from script names.
- Cost per run is shown but not capped; `--max-turns` is the only guard.
- `runs/` grows forever.
- `vscode://` links need VS Code; PyCharm users copy the path.
- Google Fonts needs internet; without it the system fonts are used.

---

## 12. How to test without spending tokens

**Replay a real run.** `runs/` is not served over HTTP, so paste the events in by hand. Open the page, and in the browser console run:
```js
const lines = `<paste the contents of runs/<id>/events.ndjson here>`.trim().split("\n");
lines.forEach(l => handle(JSON.parse(l)));
```
This renders the log, stages and result exactly as the tester saw them. Use it for any UI change.

**Stand-in `claude`.** Put an executable named `claude` earlier on PATH that:
- reads stream-json user messages from stdin on a background thread (the first message is the prompt, starting with `/<skill>`);
- prints scripted stream-json lines with small sleeps: an `init` event with `session_id` and `slash_commands`, `assistant` tool_use and text events, `user` tool_result events, then a `result`;
- at each tool result, checks for queued tester messages and acknowledges them (mid-turn);
- after the `result`, waits for the next message (a new turn: `init` again, some work, a `result` with **per-message** `num_turns` and **cumulative** `total_cost_usd`) and exits on stdin EOF;
- creates or edits files in its cwd to simulate a new or modified test;
- picks a scenario from a keyword in the first message.

Start the server with that PATH:
```bash
PATH=/path/to/stub-bin:$PATH PORT=8799 python3 testgen-ui/app.py
```
Cover these scenarios: not covered, partial, covered, won't compile, turn limit (`subtype: error_max_turns`, non-zero exit), skill missing (`slash_commands` without the skill), message mid-turn, message after a turn, Finish, idle timeout (set `TESTGEN_IDLE_MIN=0.25`).

**Regression checklist** after any change:
1. All six outcomes in §9 render correctly.
2. A 409 for Generate while a session is open, and for a message to an ended session.
3. A 400 for a missing screenshot.
4. A closed tab still produces `result.json` and ends the session (`tab closed`).
5. A mid-turn message and an after-turn message both show `› you:` in the log, and Claude acts on them.
6. `git status` stays clean apart from the generated test files.
7. One real run with one follow-up message, about $0.25.

---

## 13. Improvement backlog (each step keeps the design in §0–§1)

Ordered by value for effort. Each item says **where** the change goes.

1. **Exact stage markers.** *Where:* SKILL.md, plus a small `classify` change. Have the skill print `STAGE: coverage|auth|dom|write` (for example `echo "STAGE: dom"` before each step). Map those first and keep the current rules as a fallback. Removes the dependence on script names.
2. **Environment dropdown from the real test setup.** *Where:* `app.py` reads the environment list from the repo's config file at startup and substitutes it into the datalist. Prevents typos that send auth to the wrong place.
3. **Skill-side checkpoints.** *Where:* SKILL.md only. Now that testers can reply, let the skill stop at real decision points ("DOM dump has 3 Save buttons. Which one?" or "Log in in the browser window, then reply *done*"). No app change needed.
4. **Hard stop button.** *Where:* `app.py`. A `POST /stop` calls `Session.proc.terminate()` (unlike Finish, which waits for the current work), and `testgen_end` reports `stopped by tester`.
5. **Run history.** *Where:* `GET /runs` returns `result.json` summaries; the page lists them and replays `events.ndjson` through `handle()`. Read-only, reusing the existing renderer.
6. **Multiple testers on one server.** *Where:* `app.py` creates a `git worktree` per run (`runs/<id>/wt`) and uses it as cwd, then drops the global lock. Changed files are then the worktree's diff. Needs a policy for how generated tests get back to the main branch.
7. **Cost cap.** Keep `--max-turns`. Check whether your CLI version has a spending-cap flag for `-p` before relying on one.
8. **Skill outside the repo.** *Where:* `app.py` accepts a path to a skill folder or SKILL.md, builds a temporary plugin folder (§10), and invokes `/testgen:<folder>`. Only if the skill stops living in the repo.
9. **Agent SDK backend.** Only if you need what the CLI can't give: in-process hooks, custom tools, or very fine-grained control. Keep the same event shape to the browser so `index.html` doesn't change.

### Out of scope (proposals that conflict with §0–§1)
- Splitting the run into separate LLM calls per stage, or "vision model then codegen model".
- Re-implementing the coverage, auth or DOM steps in `app.py`.
- Spawning a new Claude process per tester message. Messages go into the live session (§5, `/message`).
- Switching to a small or local model to save cost. Fix cost with caps, prompt quality and fewer retries instead.
- Adding a framework, a database or a build step to the frontend.
- Hosted Skills API or code execution as the runtime: no network access to test environments.
