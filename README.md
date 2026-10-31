# Canvas Hub

A personal, local app that collects your Canvas course information through your own interactive sign-in. It needs no API token and no AI service. It keeps the information with full history, lets you search it and track deadlines, and is honest about what it has and hasn't read.

- **Any Canvas:** paste your Canvas address or any course link, including institution-owned domains such as `canvas.ust.hk`.
- **Real sign-in:** a separate browser window opens on your institution's normal login page, where SSO and MFA work as usual. Canvas Hub never sees your password. It only reports "signed in" after Canvas itself confirms who you are.
- **Collects what your courses actually show:**
  - discovery starts from **Courses → All Courses**, then follows each course's own navigation plus the links in Home, the syllabus, modules and pages;
  - it reads announcements, assignments with your submission state, grades and feedback, modules, pages, files (with text extracted from PDF, Office and EPUB), discussions (staff answers) and the calendar;
  - groups and Inbox are optional.
- **Read-only toward Canvas unless you confirm a write:**
  - By default it never submits, posts, sends, starts quizzes, accepts invitations or changes settings.
  - Module items that would be marked complete just by viewing them are listed but not opened unless you allow it.
  - If you switch on **Writing to Canvas** (Data & settings), the assistant can *prepare* an assignment submission (text or file), a discussion reply or an inbox message. You see the exact preview and press **Send** yourself; nothing goes out otherwise. Every write is logged.
- **History you can trust:**
  - every field keeps when it was last read;
  - real edits are kept separate from "read for the first time";
  - nothing is called deleted just because a partial check missed it;
  - your notes and local to-dos are never overwritten.
- **Coverage page:** shows per course and per category what was read completely, partially, or not at all, and why.
- **Offline use:** everything collected stays readable offline, and backup, restore and portable export are built in. Browser sign-in data is never included in backups or exports.

Implementation brief: [docs/CLAUDE_BRIEF.md](docs/CLAUDE_BRIEF.md). Integration contract for Codex: [CODEX_HANDOFF.md](CODEX_HANDOFF.md).

## Status

The main application is built and tested against a synthetic Canvas, both with automated tests and with real Chromium end-to-end runs. Verification against real HKUST Canvas needs your sign-in and is still pending: see [docs/REAL_CANVAS_ACCEPTANCE.md](docs/REAL_CANVAS_ACCEPTANCE.md).

## Install and run

Install [uv](https://docs.astral.sh/uv/), which also installs a suitable Python.

On macOS:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

On Windows, in PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Start Canvas Hub by double-clicking the launcher for your computer:

- macOS: `scripts/start-canvas-hub.command`
- Windows: `scripts/start-canvas-hub.cmd`

The first run installs the Python dependencies and Playwright's Chromium browser (~150 MB). Your default browser opens the app at `http://127.0.0.1:8730/?token=…`. The link contains a private local access token.

You can also start it from a terminal:

```bash
uv sync
uv run playwright install chromium     # first time only
uv run canvas-hub serve
```

In the app:

1. Paste your Canvas address.
2. Click **Open sign-in window** and sign in there.
3. Click **Start baseline collection**.

After that, use **Quick check** for fast updates and **Thorough refresh** to re-read everything.

If your institution's SSO doesn't like Playwright's Chromium, use your installed Google Chrome with the app's own separate profile:

```bash
CANVAS_HUB_BROWSER_CHANNEL=chrome uv run canvas-hub serve
```

In Windows PowerShell, set the variable first:

```powershell
$env:CANVAS_HUB_BROWSER_CHANNEL="chrome"
uv run canvas-hub serve
```

To pass server options through the Windows launcher, such as a different port:

```powershell
.\scripts\start-canvas-hub.cmd --port 8740
```

### Try it without a Canvas account

```bash
uv run canvas-hub fake-canvas --port 8765    # synthetic data only
```

In another terminal, run `uv run canvas-hub --home /tmp/canvas-hub-demo serve` on macOS, or `uv run canvas-hub --home "$env:TEMP\canvas-hub-demo" serve` in Windows PowerShell. Paste `http://127.0.0.1:8765/`, and sign in with the test credentials the fake server prints.

## Assistant and other agents

- **Assistant page:** chat with your Canvas data using your own API key. It has a **Chat** tab, where only the conversation scrolls, and a **Settings** tab, where you can save several setups (for example DeepSeek and Claude). Next to the message box you pick the setup and the reasoning effort for each message; the reasoning level is not part of a saved setup.
  - **Supported providers:** Anthropic (Claude), OpenAI, Google Gemini, OpenRouter, DeepSeek, a local Ollama model, or any OpenAI-compatible endpoint.
  - **What it can do:** answer from the stored data. If you allow it, it can also check Canvas for updates through the app's signed-in browser (with the same read-only rules as Quick check) and create or edit your local notes and to-dos.
  - **Your key:** stays in a private file on this computer and is never included in backups or exports. Your questions, and the course data the assistant reads, go to the provider you choose.
- **MCP:** other agents such as Codex, Claude Code or Claude Desktop can use your data through a local MCP server:

  ```bash
  uv run canvas-hub mcp                 # read-only
  uv run canvas-hub mcp --allow-writes  # also notes and Canvas checks
  ```

  The exact command for your installation is shown under **Data & settings → Connect other agents**.
- **Interactive answers:** instead of plain text, the assistant can show:
  - deadline cards with live countdowns;
  - a week timetable;
  - tables you can sort, filter, total and group;
  - stacked charts and progress gauges with a pass line;
  - a side-by-side diff of edits;
  - a JSON tree;
  - an **artifact block** for finished text, with Copy and Download.

  Everything can be copied as Markdown, CSV or JSON, downloaded, or shown full screen. Click an item to see its full content in a side panel.
- **Staying current:**
  - **Automatic refresh** (Collection) runs a quick check every 30 minutes to 4 hours while the app runs and you're signed in. It is off by default and never opens a sign-in window.
  - The 🔔 bell lists new announcements and assignments, changed due dates, newly posted grades and items that disappeared. Desktop notifications are optional.
  - Exam dates found in course pages appear under **Deadlines** for you to confirm.
  - A timetable `.ics` can be imported into the Planner. Each saved class time repeats every week; fortnightly events are skipped and reported.
- **Files:** finished work the assistant writes can be saved under **Notes & tasks → Files**, downloaded, or attached to a prepared submission. Uploaded UTF-8 text files (such as Markdown, CSV and JSON) can also be read back by the assistant.
- **Trackers page:** the assistant can build a **score model** for a course, with each assessed line, its points, gating rules (all-or-nothing, minimum hours) and the pass mark. Untick lines you plan to skip and the total, ceiling and margin update immediately. It can also build a **checklist** that can be linked to your to-dos. Both are saved, with undo, and the assistant can read back what you ticked.
- **Grade projections and hours:** the assistant can build a **grade projector** for a course. You enter what-if scores and see your letter, and the score you need on the rest for each letter. It can also build an **hours tracker** for service hours, where nothing counts until the minimum and the line is lost if the deadline passes below it. Both show warnings when a plan is at risk.
- **To-dos:** to do / in progress / blocked / done, with a **Board** view you can drag cards across. Click any title or body to edit it, and tick `- [ ]` lines in place.
- **Reminders:** the ⏰ button on a deadline or to-do reminds you inside the app (and optionally with a desktop notification) while it is open.
- **More views:** a term timeline of modules and exams, and a workload heatmap. Anything with dates can be exported as `.ics`; any component can be saved as an HTML snapshot or printed to PDF.
- **Grading rules and formulas:** Canvas usually reports 0 for every group weight. Each course page has a **Grading rules** card for the syllabus weights and the pass mark, and the assistant can also save rules Canvas lacks (minimum hours, all-or-nothing lines, a formula such as `if(hours >= 10, 20 * min(1, hours / 20), 0)`). Score models and projections use them; they stay on this computer.
- **All courses at a glance:** the Overview shows every course on one scale: grade so far, work done, what is lost, and what is due next.
- **Custom blocks (advanced, off by default):** when no built-in view fits, the assistant can write its own HTML. It runs only when you press **Run**, in an isolated frame with no network access.
- **Text from scans and photos (optional):** with [Tesseract](https://tesseract-ocr.github.io/) and [Poppler](https://poppler.freedesktop.org/) installed, Canvas Hub can read photographed forms and scanned PDFs. Install `tesseract poppler` with Homebrew on macOS, or the Windows builds and add their program folders to `PATH` on Windows. Each document shows a confidence score, and low-confidence text is flagged.
- **Planner page:** add your class times, see your week with deadlines and to-dos, get suggested study blocks, and export to Apple Calendar (`.ics`). It's a local plan only.
- **Agent requests page:** for sites outside Canvas (like a separate course website), leave a request that a connected agent with its own browser access can fulfil. Run the MCP server with `--allow-fulfil` for that agent. The text it sends back is stored, searchable, and labelled as agent-provided.
- **Direct Codex connection:** the assistant can use your locally installed Codex CLI and its existing login, with the same Canvas Hub tools and permissions. No Codex credentials are saved in Canvas Hub.

### Use Codex in the assistant

1. Install Codex CLI **0.163.0 or newer** on the computer running Canvas Hub, then sign in there:

   ```bash
   npm install -g @openai/codex
   codex login
   codex login status
   ```

2. Under **Assistant → Settings → Add setup**, choose **Codex (your Codex CLI login)**. **Model** lists the models your Codex CLI offers; keep **Codex default** or pick one. Choose **Save & test**, then **Use this setup** or select it beside the chat message box. Next to the message box you can also switch to another Codex model for a single message, and choose the reasoning effort, which offers only the levels that model supports.

Codex runs through its local app-server. Questions and the course data it reads are sent to the configured Codex backend. Each assistant run uses an ephemeral Codex thread; Canvas Hub retains the chat in its own history. The adapter disables environment access, inherited MCP servers, shell tools, web search, and other agent features, and routes Canvas Hub tool calls through the existing account and permission checks. Writes to Canvas still require the user's confirmation in the app. **Stop** terminates the active Codex process, including while it is reasoning.

If the app cannot find `codex`, restart it after installing the CLI or set `CANVAS_HUB_CODEX` to the executable's full path before starting Canvas Hub. Sign-in and credentials remain managed by Codex; do not paste Codex tokens into Canvas Hub or chat. Connection errors are shown in Assistant settings.

## Where your data lives

| What | Where |
| --- | --- |
| Course data (SQLite database, downloaded originals, extracted text) | macOS: `~/Library/Application Support/CanvasHub/data/`; Windows: `%LOCALAPPDATA%\CanvasHub\data\` |
| Backups and exports | `…/CanvasHub/data/backups/` |
| App browser profiles (your Canvas sign-in) | `…/CanvasHub/browser-profiles/` (separate; never backed up or exported) |

Run `uv run canvas-hub paths` to print the exact locations. To use a different folder, set `CANVAS_HUB_HOME` or pass `--home`.

- **Data & settings** in the app has backup, verify, restore, portable export and import.
- **Sign out** and **Clear saved sign-in** remove only the browser session.
- **Delete stored course data** is a separate action that needs you to type `DELETE`.

## How it works, and why

- **Local companion process.** A Python (FastAPI) server bound to `127.0.0.1` serves the UI. It requires a per-installation token, checks Host and Origin headers, and sends a strict Content-Security-Policy. Collected HTML is sanitized before storage and treated as untrusted.
- **Real browser, app-owned profile.** Playwright drives a persistent Chromium profile that belongs only to Canvas Hub. Nothing is imported from your other browsers. Your web UI can't fetch Canvas across origins and SSO can't run inside an embedded frame, so a real browser process is the only robust way to sign in.
- **Reading Canvas.** All Courses is read from the rendered page. Course content is read with same-origin GET requests to Canvas's documented REST API, sent from that signed-in browser, which is exactly how Canvas's own pages load data.
  - This gives exact timestamps with time zones, complete pagination and stable IDs, which page-scraping can't promise.
  - The tradeoff is reliance on the API being available to browser sessions, which Canvas's own UI depends on.
  - To respect each course's structure, a section is read only when that course's navigation shows it or its content links to it.
- **Storage.** SQLite with numbered migrations, FTS5 full-text search, and a content-addressed folder for originals. Every Canvas fact is an observation with its own read time. Revisions and change events are derived from observations, so a re-run or re-import never duplicates anything or invents changes.
- **Documents.** [MarkItDown](https://github.com/microsoft/markitdown) extracts text locally (PDF, DOCX, PPTX, XLSX/XLS, EPUB, HTML, text). Scanned PDFs, images and audio/video are labelled clearly rather than reported as read. Nothing is sent to a remote service.
- **Jobs.** Collection is a persisted task queue. It survives app restarts, pauses when your session expires, resumes after you sign in again, and can be paused or cancelled. Only one job runs per account, with polite request spacing.

## Development

```bash
uv sync
uv run pytest -q                    # all tests, including real-Chromium end-to-end runs (~35 s)
uv run pytest -q -m "not browser"   # fast tests (~2 s)
```

To use an existing Chromium installation for both collection and browser tests, set `CANVAS_HUB_CHROMIUM` to its executable path before running these commands.

Code map:

| Path | Contents |
| --- | --- |
| `canvas_hub/boundary.py` | Service API used by the UI and the future Codex adapter |
| `canvas_hub/services/` | Storage and domain logic |
| `canvas_hub/collector/` | Browser, sign-in, traversal, job runner |
| `canvas_hub/web/` | HTTP server and UI (no build step) |
| `canvas_hub/testing/fake_canvas.py` | Synthetic Canvas for tests |

Keep credentials, browser profiles, real course data and databases out of git; `.gitignore` covers the defaults.
