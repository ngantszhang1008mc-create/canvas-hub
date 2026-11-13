# External links: inspecting sites outside Canvas

Courses often keep their real material somewhere else: a course website, a
submission or grading system, a discussion forum. Canvas Hub records every such
link it finds during collection. This page describes how it reads them.

## What you see

1. **A notice when links are found.** After a collection, a small notice says
   how many new external links were found and asks whether Canvas Hub may open
   them. Finding a link never opens it. **Inspect all** starts the reads,
   **Review first** shows the list, **Not now** hides the notice until another
   new link turns up.
2. **One list, one status per link.** *External links* in the sidebar lists
   every link by course. The same rows appear on each course page and on the
   item that contains the link. Each row shows what happened last time and the
   next step.
3. **Inspect.** Canvas Hub opens the link in its own background browser, waits
   for the page to finish drawing, and saves the text. The saved page is
   searchable, has its own item page (**View saved page**) and can be read by
   the Assistant. No AI provider is involved in an inspection.
   Many course systems (graders such as Zinc, forums) draw an almost empty page
   and fill it from their own API. Canvas Hub also saves the data the page
   itself loaded from its own site (as readable "name: value" lines), so
   deadlines, scores and other details are kept even when the visible text
   leaves them out. Sign-in and session responses are skipped, and fields that
   look like secrets (tokens, keys, passwords) are dropped.
4. **Decide once per site.** Most links point at a few sites, and most of
   those carry nothing about the course. The **Sites** card on the External
   links page lists every site with its number of links:
   - **Ask each time** (the default): new links are announced as above.
   - **Always read**: its links are read without asking, including links a
     later collection finds.
   - **Ignore**: its links are hidden from the lists and the notice and are
     never opened. **Ignore this site** on a row does the same in one press.

   Video, social media, encyclopedia, programming-reference and meeting sites
   (YouTube, Wikipedia, Stack Overflow, Zoom, ...) start as ignored; choose
   **Ask each time** to bring one back.

| Status | Meaning | What to do |
| --- | --- | --- |
| Read and saved | The page was readable. | Nothing. **Inspect again** re-reads it. |
| Sign-in needed | The site wants you to sign in. The row says which kind. | Follow the row (see below). |
| Network problem | Offline, address not found, timeout, refused, certificate problem. The message says which. | Check the connection or VPN, then **Try again**. |
| Page not found | 404 or 410. | The link in Canvas is probably out of date. |
| Access refused | 403. | Sign in if you have not; otherwise your account has no access. |
| Site error | 5xx or rate limiting. | Try later. |
| Blocked by the site | A robot check. | **Open the site in a window**, pass the check, press Continue. |
| Nothing readable | The page loaded with no text. | Often means a sign-in is needed. |
| Unsupported file | Not HTML, PDF or plain text. | Open it in your own browser. |
| Not opened | A private-network address, or a link that looks like an action (log out, submit, delete). | Canvas Hub will not open these. |

When the computer cannot reach Canvas either, a network failure is reported as
"this computer looks offline" rather than blaming the site.

## Signing in

Canvas Hub decides which kind of sign-in a site uses from what the browser
actually received, and offers exactly one way in.

### 1. Username and password prompt (HTTP Basic)

*Example: a course website that pops up the browser's own username/password box.*

The row shows a small form: username, password, an optional **Remember on this
computer**, and **Sign in and continue**. The values go from that form to the
local Canvas Hub process and from there only to that one site origin, and only
over https. They are not sent to Canvas or to an AI provider and are never
written to the database, logs, backups or exports. Without *Remember* they are
kept in memory until Canvas Hub closes. With it they are stored in
`<home>/runtime/site-logins.json` (owner-only, the same folder as the API token
and assistant keys). A password the site rejects is discarded immediately, so it
is never sent twice. If the site redirects to another origin, the password is not
sent with that redirect; redirects within the same site keep it.

### 2. University single sign-on (redirect to another sign-in host)

*Example: a grading system that sends you to your university's login page,
possibly followed by a phone or code prompt.*

**Open sign-in window** opens a separate Canvas Hub browser window on that
page. Sign in there exactly as you normally do. Canvas Hub cannot see what you
type. When the course page appears, Canvas Hub reads it and closes the window
by itself. If it does not notice, press **I've signed in — continue**.

### 3. The site's own sign-in page

*Example: a discussion forum with an e-mail and password form.*

Same window, same steps. Pages that are drawn by a script after loading are
read once they have finished drawing.

### After you sign in

- The read continues without another click.
- Other links that needed the same sign-in (the same site, or the same
  single sign-on) are read automatically. Their rows say "Needs the same
  sign-in as …" instead of asking again.
- Only one sign-in window is open at a time. While it is open, other reads
  wait, so no tabs appear in the window you are typing in.
- A sign-in that the site later ends shows up as *Sign-in needed* again. The
  page saved earlier is kept.

**Forget site sign-ins** (External links page) deletes the external-site
browser profile and any remembered passwords. Saved pages are kept.

## What Canvas Hub does and does not do

- It opens a link only when you ask, and it only reads: it never clicks,
  types, submits or follows links on the page.
- The browser profile for external sites belongs to Canvas Hub and to one
  Canvas account. It is separate from the Canvas sign-in profile and from your
  own browsers, lives under `<home>/browser-profiles/external-sites/`, and is
  never part of a backup or export.
- Addresses on a private network and links that look like actions are refused.
  The inspection browser makes every connection through a small local proxy
  that resolves the host name and refuses private and local addresses. That
  covers each redirect hop and every image, script or other resource a page
  loads, not only the link itself. If a public link redirects to a private
  address, nothing is saved.
- Saved text is website content. The app and the Assistant treat it as data,
  not as instructions, and label it as read by Canvas Hub rather than reported
  by Canvas.
- HTML pages, PDFs (first 20 pages, up to 1 MB, no OCR) and plain text are
  read. Up to 200,000 characters are kept per page.
- Reading a page can have the same side effects as opening it yourself, for
  example a forum marking a post as seen.

## The Assistant

`list_external_sources` shows each link with its inspection status and the id
of the saved page. `inspect_external_link` (needs the *check Canvas for
updates* permission) asks the app to open one link and returns the outcome. If
a sign-in is needed, the tool says so and the Assistant asks you to sign in
from the External links page; it never asks for a password in chat. Over MCP
from another program there is no browser, so the tool explains that the link
has to be inspected in the app.

## Try it without real accounts

```bash
uv run python scripts/preview-link-inspection.py
```

This starts three made-up sites, one for each kind of sign-in, plus a public
page and a broken link, and a throwaway Canvas Hub that links to them. The
terminal prints the made-up usernames and passwords. Stop it with Ctrl-C;
nothing is kept.

## What has been verified

Everything above is covered by automated tests against those synthetic sites
with real Chromium (`tests/test_link_inspection.py`,
`tests/test_link_inspection_ui.py`).

The real sites have **not** been exercised: the build environment cannot reach
them and nobody could sign in. An earlier anonymous check (3 November 2026)
saw `http://course.cse.ust.hk/comp1023/` redirect to https and the https page
answer 401 with a Basic challenge, which is sign-in kind 1. Oxygen/Zinc and
Piazza are expected to be kinds 2 and 3 but that is unconfirmed. See the
checklist in [REAL_CANVAS_ACCEPTANCE.md](REAL_CANVAS_ACCEPTANCE.md#7-external-links-course-website-oxygenzinc-piazza).

Known limits:

- A single sign-on provider may behave differently in an automated browser
  than in your everyday one. If the bundled Chromium is refused, start Canvas
  Hub with `CANVAS_HUB_BROWSER_CHANNEL=chrome`, as for Canvas itself.
- Sites that keep their session only in a session cookie need a new sign-in
  after Canvas Hub restarts.
- A site that blocks background (headless) browsers is reported as blocked.
  It can still be read through the sign-in window.
- Piazza reached through a Canvas tool launch (not a plain piazza.com link) is
  recorded as a Canvas tool and is not listed here.

## Earlier analysis features (paused)

Before link inspection, external links went through an AI classification step
with optional declarative adapters, and a connected MCP agent could read a
site on your behalf. That path is still paused in code
(`canvas_hub/external_inspection.py`) and has no controls in the app.
Classifications, adapters and text saved by it are kept; text saved earlier is
shown on the link's row. The restricted site sign-in that accompanied it was
replaced by the sign-in described above and has been removed. Details of the
paused path are in [EXTERNAL_SOURCES.md](EXTERNAL_SOURCES.md).
