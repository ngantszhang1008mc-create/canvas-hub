# Real-Canvas acceptance checklist (needs you, on your Mac)

Everything below needs your real institutional sign-in, so it could not be verified in the build environment. The automated tests cover the same behaviours against a synthetic Canvas.

You sign in yourself in the window Canvas Hub opens. Never paste passwords, cookies or tokens anywhere else, including into chat.

Record each result as **pass**, **fail** (what you saw), or **n/a**. Once you've worked through the list, give Claude the results, plus `canvas-hub status` output with names redacted if you prefer. Claude can then fix anything that differs from the synthetic Canvas.

## 0. Install and start

```bash
cd canvas-hub
./scripts/start-canvas-hub.command
```

Alternatively run `uv sync`, then `uv run playwright install chromium`, then `uv run canvas-hub serve`.

The UI opens in your default browser. If it doesn't, copy the printed `http://127.0.0.1:8730/?token=…` link.

## 1. Address and sign-in (brief §3)

| # | Step | Expected |
| --- | --- | --- |
| 1.1 | Paste `https://canvas.ust.hk/courses/73682/assignments/456957` and save | Origin shows `https://canvas.ust.hk` |
| 1.2 | Click **Open sign-in window** | A separate Chromium window opens on HKUST's login page. The top bar says *Waiting for sign-in*, not *Signed in*. |
| 1.3 | Complete SSO and MFA in that window | Within a few seconds the setup page shows **Verified: \<your name\>**, your user id, time zone **Asia/Hong_Kong**, and no warning that the course link is inaccessible |
| 1.4 | Quit Canvas Hub (Ctrl-C in Terminal) and start it again | Either *Signed in* (persistent cookies) or *Session expired / Not signed in*. Note which. Data from earlier is still shown either way. |
| 1.5 | If 1.4 needed sign-in: click **Sign in** | SSO may pass straight through; then *Signed in* again |
| 1.6 | (Optional) If SSO blocks the bundled Chromium | Quit, run `CANVAS_HUB_BROWSER_CHANNEL=chrome uv run canvas-hub serve` (uses Google Chrome with the app's own profile), and retry 1.2–1.3 |

## 2. Baseline collection (brief §4, §11)

Under **Collection**, keep the default scope (current courses) and click **Start baseline**. Watch the job progress. A full baseline of about ten courses with file downloads can take a while, because requests are deliberately spaced out.

| # | Check | Expected (from the 30 Oct observations) |
| --- | --- | --- |
| 2.1 | **Courses** page | All 11 enrollments from All Courses. The unpublished one shows *Not accessible* or *unpublished* and was not opened. No past-course section if your account has none. |
| 2.2 | MATH1023 (73682) → **Coverage** row | Modules and Quizzes show *Not in navigation*. Announcements *complete* with 35 items (or the current count). Files complete, or partial with a stated reason. |
| 2.3 | Open assignment 456957 | Instructions, availability dates, and your submission time with late state, each showing when it was read |
| 2.4 | MATH1023 files: open a file in a sub-folder | The original downloads and the extracted text shows (or a clear *needs OCR* / *image only* label) |
| 2.5 | COMP2711H (71968) | Pages/Files show *Not in navigation*, but lecture notes and files linked from Home appear under the course and are searchable |
| 2.6 | AISC1000A (72312) | The Home text includes the collapsed section. Modules list mixed items, future unlock dates and prerequisites. View-to-complete items are listed but **not opened**, and the Coverage row says so. |
| 2.7 | In Canvas itself, check AISC1000A module progress | No view-to-complete requirement changed because of the collection |
| 2.8 | **Deadlines** | Due times in HKT, matching Canvas. Any undated or ambiguous dates appear under "Dates that could not be placed". |
| 2.9 | **Coverage** page | Every gap has a reason (locked, hidden until you post, linked tool not launched, …) |
| 2.10 | **Search** for a phrase you know is inside a lecture PDF | The document hit links back to the item and to Canvas |

## 3. Updates and history (brief §8)

| # | Step | Expected |
| --- | --- | --- |
| 3.1 | Immediately run **Quick check** | Finishes. **Changes** shows nothing new (or only real changes since the baseline). No duplicate items. |
| 3.2 | After a real instructor change (new announcement, edited due date) run **Quick check** | It appears as *First seen* or *Changed*, with old → new values |
| 3.3 | Run **Thorough refresh** | No false changes. Anything that disappeared is *Not in latest list*, then *Source unavailable* only after its own page was re-checked. |

## 4. Session expiry and resume (brief §3, §8)

| # | Step | Expected |
| --- | --- | --- |
| 4.1 | Start a **Thorough refresh**, then while it runs click **Data & settings → Sign out** (or wait for Canvas to time out) | If the browser is busy, the app asks you to pause first. Pause, sign out, then resume: the job shows **Needs sign-in**, and stored data stays visible. |
| 4.2 | Sign in again | The job resumes by itself from its checkpoint and finishes |
| 4.3 | Quit the app mid-job, then restart | The job shows **Interrupted**. **Resume** continues it. |

## 5. Data safety (brief §9, §12)

| # | Step | Expected |
| --- | --- | --- |
| 5.1 | Add a note and a local to-do on an assignment, then run **Quick check** | Both are unchanged. The Canvas submission state did not change. |
| 5.2 | **Create backup**, then **Verify** | OK, with item counts |
| 5.3 | Unzip the backup and search it for your cookie or session names | Not present (it holds only `database/`, `resources/`, `manifest.json`) |
| 5.4 | **Sign out** | Course data is still visible offline |
| 5.5 | Turn off Wi-Fi and browse Deadlines, Search and an item | Works from stored data; collection buttons say they need Canvas access |

## 6. Nothing was written to Canvas (brief §5)

After the runs above, check in Canvas:
- no submissions, quiz attempts, discussion posts or messages were created;
- notification and profile settings are unchanged;
- favorites and the dashboard are unchanged;
- invitations were not accepted.

## 7. COMP1023 external website (brief §3)

Canvas Hub does not open the separate COMP1023 course website and never imports your Chrome profile `ngan`. If you want that site's content in Canvas Hub later, the documented route is user-controlled. You open it in your own Chrome profile, and a trusted collector produces an observation package that you import, under the same identity checks as the built-in collector. That collector could be the future Codex Canvas Reader adaptation. See `CODEX_HANDOFF.md` §5.

## 8. Assistant and MCP (needs your own API key for 8.1–8.4)

Don't paste your key into chat; enter it only in the app.

| # | Step | Expected |
| --- | --- | --- |
| 8.1 | **Assistant → Settings**: pick a provider, paste your key, click **Save & test** | Shows *Connected* with the model name. Your earlier setup appears under **Saved setups**. |
| 8.1b | **+ Add setup** for a second provider, then pick it in the selector next to the message box | The next answer comes from the chosen setup (shown under your message); the other setup's key stays saved |
| 8.1c | Ask several questions in one chat | Only the chat area scrolls; the page itself stays put and the message box stays visible |
| 8.2 | Ask "What's due in the next 7 days?" | Steps such as *Checked deadlines* appear; the answer matches the Deadlines page, in HKT |
| 8.3 | Ask "Check Canvas for new announcements in MATH1023" (while signed in) | It starts a quick check limited to that course, waits for it, then answers. The job also appears under **Collection**. |
| 8.4 | Ask "Add a to-do to start the next assignment on Saturday" | The to-do appears under **Notes & tasks**; nothing changes in Canvas |
| 8.5 | Run `uv run canvas-hub mcp` and register it in Codex or Claude Code (command shown under **Data & settings → Connect other agents**) | The agent can list your courses and deadlines. Write tools appear only with `--allow-notes` / `--allow-writes`. |
| 8.6 | DeepSeek: setup with provider **DeepSeek**, model `deepseek-flash`, **Save & test**; then in the chat pick **Reasoning: max** and ask 8.2 again | *Connected*; the multi-step answer completes and your message shows "reasoning max". If the provider rejects it, try **high** to see whether the hosted API accepts `max`. |

## 9. Planner and external-site requests

| # | Step | Expected |
| --- | --- | --- |
| 9.1 | **Planner**: add your class times (or ask the assistant to read them from a syllabus) | The week grid shows classes, deadlines and to-dos in HKT |
| 9.2 | Tick **Suggest study blocks**, click **+ Add** on one | It becomes a to-do; nothing changes in Canvas |
| 9.3 | **Export .ics** and open it in Calendar | Classes repeat weekly; deadlines appear 30 minutes before their due time |
| 9.4 | Ask the assistant "Plan my study time this week" | It answers with an interactive timetable and buttons, not just text |
| 9.5 | **Agent requests**: leave one for the COMP1023 website with the note "Use Chrome profile ngan" | Status *Waiting for an agent* |
| 9.6 | In Codex or Claude Code (MCP started with `--allow-fulfil`), ask it to fulfil open Canvas Hub requests | Status becomes *Done*; the content is searchable and labelled as agent-provided |

## 10. Score models, checklists and artifacts

| # | Step | Expected |
| --- | --- | --- |
| 10.1 | Ask "Build a score model for HMAW1905B with the pass mark from the syllabus" | It shows all assessed lines. Booked and ceiling match your own count (e.g. 15 booked, 95 ceiling), and lost lines (e.g. Community Meeting 1, 0/10) are marked *Lost* |
| 10.2 | Untick a line you plan to skip (e.g. Nest Posts), then another (e.g. FYES) | The total and margin update immediately (e.g. 85, then exactly 80 with +0 margin against an 80 pass mark) |
| 10.3 | Reload the page, open **Trackers** after pinning it, then ask "Which lines am I skipping?" | The ticks are kept, and the assistant names the unticked lines |
| 10.4 | Ask for a checklist of this week's tasks linked to your to-dos; tick one; then mark another done under **Notes & tasks** | Both stay in step; nothing changes in Canvas |
| 10.5 | Ask the assistant to write something long for you (a plan or a spec) | It appears in an artifact block; **Copy** puts the full text on the clipboard and **Download** saves the file |
| 10.6 | On any table or chart, use **Copy ▾ → Copy as CSV**, paste into Numbers or Excel | The rows paste as columns |

## 11. Confirmed writes, freshness and dates (needs your sign-in)

Writing to Canvas is real: use a low-stakes assignment or a draft.

| # | Step | Expected |
| --- | --- | --- |
| 11.1 | With writes off, ask the assistant to submit something | It says writing is off, and nothing is prepared |
| 11.2 | **Data & settings → Writing to Canvas**: switch on. Ask it to prepare your Study Plan upload (it saves the file first) | A *Prepared for Canvas* card shows the course, assignment, file name, size and SHA-256. Nothing appears in Canvas yet |
| 11.3 | Press **Send to Canvas… → Yes, send it** | *Sent to Canvas* with the attempt number. Canvas shows the submission. The write log and **Changes → What you sent** list it |
| 11.4 | Prepare a reply, then press **Cancel**; prepare another and leave it for 24 hours | *Cancelled* and *Expired*; neither is sent |
| 11.5 | **Collection → Automatic refresh: every hour**; leave the app open | Quick checks appear in Jobs about hourly; the 🔔 bell lists new grades or announcements |
| 11.6 | **Include Inbox**, run a quick check | Instructor and group messages appear (sender, unread state), and Canvas still shows them as unread |
| 11.7 | **Deadlines → Dates found in course pages** | The COMP2711H midterm (Rm 2503/2504, 19:00–22:00) is suggested. **Confirm** puts it in the Planner |
| 11.8 | **Courses** | AISC1000A / COMP4900 / LANG1406 duplicates show *Possible duplicate*; TEMG1011 shows *Not accessible*; empty or hidden shells are labelled |
| 11.9 | Start `canvas-hub mcp --allow-fulfil` in Codex and ask it to serve requests | **Agent requests** shows Codex as connected; an open COMP1023 request is claimed |

## 12. Projections, hours, board and reminders

| # | Step | Expected |
| --- | --- | --- |
| 12.1 | Ask "Project my grade in <course> — the syllabus weights are …" | A grade projector with your graded items. The current percent matches Canvas's (graded categories only). Each letter shows the score needed on the rest, and an unreachable letter shows "over 100" |
| 12.2 | Drag a what-if slider, or press **Assume on the rest: 80%** | The percent and letter update at once and are kept after a reload |
| 12.3 | Ask for an SDE service-hours tracker (20 h, 10 h minimum, deadline from the syllabus), then log your hours | Below 10 h it shows *0 until 10 h* and an alert. It shows the hours needed per week and when you last checked Engage |
| 12.4 | **Notes & tasks → Board**: move a to-do to *In progress* and another to *Blocked* | The states are kept, and the assistant's `show_kanban` shows the same board |
| 12.5 | On a Deadlines row press ⏰ → **In 1 hour** (or a custom time a few minutes away) and leave the app open | A toast (and a desktop notification if enabled) appears once at that time |
| 12.6 | Ask for a timeline of a course's modules and a workload heatmap | Locked modules show their unlock date and prerequisites; busy days are darker |
| 12.7 | On a deadlines card choose **Copy ▾ → Download .ics (calendar)**, then open it in Calendar; on the projector choose **Print / Save as PDF** | The events import; the PDF contains only that component |

## 13. Formulas, grading rules, dashboard, custom blocks and OCR

| # | Step | Expected |
| --- | --- | --- |
| 13.1 | On a course page, open **Grading rules**, enter the syllabus weights (e.g. HMAW1905B) and the pass mark, then **Save rules** | The weights add up to 100% (otherwise a red note). Asking for a grade projection of that course uses them |
| 13.2 | Ask "Track my SDE hours in the score model; 0 until 10 h, then 20 × hours / 20" | The SDE line shows the formula. Entering 8.5 h gives 0 and 15 h gives 15, both on the page and in the assistant's read-back |
| 13.3 | **Overview** | **All courses** lists each course with grade so far, work done, lost lines and the next deadline; course names open the course |
| 13.4 | **Data & settings → Custom HTML blocks**: switch on. Ask for "a custom chart of my hours by week" | A block appears with **Run**; nothing runs before you press it. After **Run** the chart shows, and the block cannot load anything from the internet |
| 13.5 | Install tesseract (`brew install tesseract poppler`), restart, then **Data & settings → Text from scans and photos**: switch on and press **Read … stored documents** | Photographed forms and scanned problem sheets get text with a confidence score. Low-confidence ones say *OCR, low confidence*. Their words become searchable |
| 13.6 | With VoiceOver (⌘F5), tab through **Trackers** and the workload heatmap | Every control is announced by name, and heatmap days with deadlines read out what is due |
