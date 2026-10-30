# Canvas Hub

A personal, local app that collects your Canvas course information through your own interactive sign-in. It needs no API token and no AI service. It keeps the information with full history, lets you search it and track deadlines, and is honest about what it has and hasn't read.

- **Any Canvas:** paste your Canvas address or any course link, including institution-owned domains such as `canvas.ust.hk`.
- **Real sign-in:** a separate browser window opens on your institution's normal login page, where SSO and MFA work as usual. Canvas Hub never sees your password. It only reports "signed in" after Canvas itself confirms who you are.
- **Collects what your courses actually show:**
  - discovery starts from **Courses → All Courses**, then follows each course's own navigation plus the links in Home, the syllabus, modules and pages;
  - it reads announcements, assignments with your submission state, grades and feedback, modules, pages, files (with text extracted from PDF, Office and EPUB), discussions (staff answers) and the calendar;
  - groups and Inbox are optional.
- **Read-only toward Canvas:** it never submits, posts, sends, starts quizzes, accepts invitations or changes settings. Module items that would be marked complete just by viewing them are listed but not opened unless you allow it.
- **History you can trust:**
  - every field keeps when it was last read;
  - real edits are kept separate from "read for the first time";
  - nothing is called deleted just because a partial check missed it;
  - your notes and local to-dos are never overwritten.
- **Coverage page:** shows per course and per category what was read completely, partially, or not at all, and why.
- **Offline use:** everything collected stays readable offline, and backup, restore and portable export are built in. Browser sign-in data is never included in backups or exports.

Implementation brief: [docs/CLAUDE_BRIEF.md](docs/CLAUDE_BRIEF.md). Integration contract for Codex: [CODEX_HANDOFF.md](CODEX_HANDOFF.md).

## Status

The main application is built and tested against a synthetic Canvas, both with automated tests and with real Chromium end-to-end runs. Verification against real HKUST Canvas needs your sign-in and is still pending: see [docs/REAL_CANVAS_ACCEPTANCE.md](docs/REAL_CANVAS_ACCEPTANCE.md). The Codex integration is intentionally not implemented; it's left for Codex.

## Install and run (macOS)

1. Install [uv](https://docs.astral.sh/uv/), which also installs a suitable Python:

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. Start Canvas Hub, either by double-clicking `scripts/start-canvas-hub.command` or with:

   ```bash
   uv sync
   uv run playwright install chromium     # first time only (~150 MB browser download)
   uv run canvas-hub serve
   ```

   Your default browser opens the app at `http://127.0.0.1:8730/?token=…`. The link contains a private local access token.

3. In the app:
   1. Paste your Canvas address.
   2. Click **Open sign-in window** and sign in there.
   3. Click **Start baseline collection**.

   After that, use **Quick check** for fast updates and **Thorough refresh** to re-read everything.

If your institution's SSO doesn't like Playwright's Chromium, use your installed Google Chrome with the app's own separate profile:

```bash
CANVAS_HUB_BROWSER_CHANNEL=chrome uv run canvas-hub serve
```

### Try it without a Canvas account

```bash
uv run canvas-hub fake-canvas --port 8765    # synthetic data only
```

In another terminal, run `uv run canvas-hub --home /tmp/canvas-hub-demo serve`, paste `http://127.0.0.1:8765/`, and sign in with the test credentials the fake server prints.

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
- **Trackers page:** the assistant can build a **score model** for a course, with each assessed line, its points, gating rules (all-or-nothing, minimum hours) and the pass mark. Untick lines you plan to skip and the total, ceiling and margin update immediately. It can also build a **checklist** that can be linked to your to-dos. Both are saved, with undo, and the assistant can read back what you ticked.
- **Planner page:** add your class times, see your week with deadlines and to-dos, get suggested study blocks, and export to Apple Calendar (`.ics`). It's a local plan only.
- **Agent requests page:** for sites outside Canvas (like a separate course website), leave a request that a connected agent with its own browser access can fulfil. Run the MCP server with `--allow-fulfil` for that agent. The text it sends back is stored, searchable, and labelled as agent-provided.
- **Direct Codex connection:** reserved for Codex to build (see `CODEX_HANDOFF.md` §6). Codex can already use the MCP server.

## Where your data lives

| What | Where |
| --- | --- |
| Course data (SQLite database, downloaded originals, extracted text) | `~/Library/Application Support/CanvasHub/data/` |
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

Code map:

| Path | Contents |
| --- | --- |
| `canvas_hub/boundary.py` | Service API used by the UI and the future Codex adapter |
| `canvas_hub/services/` | Storage and domain logic |
| `canvas_hub/collector/` | Browser, sign-in, traversal, job runner |
| `canvas_hub/web/` | HTTP server and UI (no build step) |
| `canvas_hub/testing/fake_canvas.py` | Synthetic Canvas for tests |

Keep credentials, browser profiles, real course data and databases out of git; `.gitignore` covers the defaults.
