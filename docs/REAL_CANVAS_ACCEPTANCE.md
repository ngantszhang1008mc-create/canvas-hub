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
