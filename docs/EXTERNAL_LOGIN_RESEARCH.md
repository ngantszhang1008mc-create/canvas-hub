# External site connection research

External-site connections require user review before use on real sites. They do
not import a personal browser profile, send
cookies or credentials to an Assistant, or run automatic authenticated
crawling.

## User flow

1. Select **Analyse all** in the course or item’s External sources and approve
   the displayed URLs and provider usage once.
2. Failed links are checked anonymously for authentication. A confirmed challenge
   produces **Needs login** beside that link in the current course. Unknown or
   generic access failures show **Review failure**, without claiming login is needed.
   Links already identified as requiring login are excluded from another public batch.
3. Select **Needs login**. A Basic challenge opens a labelled local username and
   password form; submitting it explicitly creates an isolated, headless connection
   and checks the exact HTTPS page. No misleading native challenge window opens.
   Browser/SSO sign-in instead shows one instruction and one next action: approve
   the displayed domains, sign in in the isolated window, then check the course page.
4. Confirm the retrieved course page, review the exact text and provider usage,
   then approve analysis. Signing in alone never sends content to a model.

Connection details, manual retries and alternate methods are collapsed. Unsupported
popups or redirects remain blocked and are explained in the current step; opening
this UI is not permission to broaden network access. Closing or leaving the form
clears its fields and prevents an operation still waiting for the connection from
submitting credentials; an already sent request cannot be recalled.
An already opened local connection remains available until explicitly closed.

Failed source inspections automatically schedule one anonymous check using a
fresh browser, without saved sessions, screenshots or model calls. The check has
a 20-second process deadline, bounded requests/bytes and at most two active
workers. It does not retry paid analysis or change saved classifications. A saved
failed inspection gets one check when reopened after an app restart. Busy or
failed checks remain unknown and offer an explicit manual retry. A 401 with an
authentication challenge, a rendered login form, and an authentication redirect
with a matching login page provide evidence of login; a generic 403, timeout,
enrollment chooser or bot challenge does not. Results appear on the matching
source row. Unknown results retain an explicit manual connection option under
**More options and diagnostics**.

The source panel also discloses a source origin and identity-provider origins
entered by the user. Only after that disclosure and explicit consent does it create a
persistent profile scoped to the Canvas account identity and site origin, and
open an interactive browser for the user's own sign-in. Unknown origins and
non-HTTPS requests are blocked. Sign-in POSTs are limited to recognized paths;
each requires separate one-time user approval and expires after 60 seconds.
Requests are routed through a public-address DNS-pinned transport, with bounded
response size. HTTP redirects are never handed to Chromium: Playwright does not
re-run route handlers for later hops in a browser redirect chain. Instead,
top-level sign-in redirects are aborted and replaced by fresh GET navigations
to validated HTTPS URLs within the explicitly approved origins. Each request
revalidates DNS and pins the connection before sending traffic, including
same-origin hops. The final document keeps its actual destination origin.
Each chain allows at most five redirects, rejects repeated URLs, and stops
starting hops after 20 seconds. Existing per-request time/size limits still
apply. New navigations use only destination-scoped cookies; source request
headers and bodies are not copied. POST 301/302/303 responses become GETs;
POST 307/308 responses are blocked because the one-time approval does not
permit replaying a submission. Subresource redirects and popup sign-in are
unsupported. Blocked redirects have a bounded reason code in the local UI,
without disclosing redirect paths, queries or authentication values.
WebSockets and service workers are
blocked. Unsupported cookie syntax and other network requests fail closed.
Both detection and connection reuse the configured `CANVAS_HUB_CHROMIUM` or
`CANVAS_HUB_BROWSER_CHANNEL` browser selection. Browser launch failures,
blocked popups, HTTP denials and redirect failures have separate fixed messages;
the UI polls connection status without resetting unchanged credential fields.

HTTP source links offer an explicitly confirmed, separate HTTPS source with the
same host/path/query. This does not overwrite the HTTP evidence, establish that
HTTPS is available, or automatically open a profile. Only HTTPS connections can
receive credentials. A Basic 401 challenge in an approved top-level document
offers a local username/password form. The local app receives these values via
its authenticated, origin-checked POST endpoint; it never requests them in chat.
The pinned transport holds an authorization value only in memory for that exact
document GET while the connection is open (including the headless Basic flow). It does not put credentials into
Chromium, its profile, application storage, logs, exports or model requests, and
does not forward them to assets, POSTs or redirect destinations. A rejected
credential clears that authorization value; closing the window also clears
queued credential entries. Reflected credential strings are removed from local
review text. Other HTTP authentication schemes remain unsupported.

Checking the requested page follows sign-in (automatically after Basic entry,
or explicitly after browser sign-in): it makes a single GET for the
exact source URL with page scripts, subresources and redirects disabled. It
shows bounded text only in the local UI, including legacy pages without a
`main` or `article` element. A user must confirm it is the requested
course page; a generic sign-in or enrollment chooser is not proof of course
access. Failure or expiry does not alter saved source data or tracker state.
After confirmation, a separate action re-reads the exact page and presents the
cleaned text, configured provider and model for explicit review. Approval
rechecks that access and the text remain unchanged before one model request.
The source origin and reviewed text are sent; credentials, browser cookies and
profiles are not supplied to the model. Common credential patterns, links and
email addresses are removed, but no automatic filter can recognize every
secret: users must review the entire exact text before approving. On success a
sanitized snapshot and classification are saved with connected-site provenance.
The snapshot can be selected as a tracker review source; changes require the
existing user review and do not automatically change grading rules or manual
state. Snapshots are local course data and are included in data backups.

No public adapter is created for an authenticated snapshot and no authenticated
background crawling is enabled. The existing public Analyze link flow remains
separate. Every connected-page analysis requires a fresh explicit approval.

Synthetic tests cover bounded GET SSO redirects, relative locations, actual
document origin, per-origin cookies, POST-to-GET without credential forwarding,
private/rebound DNS, unknown/unsafe destinations, loops and replay blocking.
Additional synthetic regressions cover automatic failure checks, Basic challenge
entry/expiry/redaction, HTTP-to-HTTPS source review, empty denial responses,
legacy course HTML, blocked popup messages and UI credential-field clearing.
Real Oxygen/Zinc sign-in has not been tested; support for this safe subset of
redirects does not establish that their full SSO flows work.

Limitations requiring real-world validation include multi-cookie and SSO flows,
login POST paths not recognized by the conservative policy, sites requiring a
Referrer header or script-rendered course pages, real HTTP Basic sign-in,
CAPTCHAs, and cross-origin assets. Such sites may remain unverified even when
the user's credentials are valid. Credentials and authentication redirects are
never printed by this module. The previous Piazza anonymous observation remains
an ambiguous login/choose-class shell, not proof that login grants course access.

`python -m canvas_hub.services.login_detector URL` makes real anonymous public
network requests. Fixture tests use synthetic responses, and the separate
screenshot research is not integrated into this branch. No live screenshot
inference or paid model call was performed here.
