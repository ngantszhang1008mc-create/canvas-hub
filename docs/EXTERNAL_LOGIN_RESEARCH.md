# External site connection research

This branch is an experimental, uninstalled vertical slice. It requires review
before use on real sites. It does not import a personal browser profile, send
cookies or credentials to an Assistant, or run automatic authenticated
crawling.

The source panel can run an explicit anonymous check using a fresh browser. It
can also disclose a source origin and the identity-provider origins entered by
the user. Only after that disclosure and explicit consent does it create a
persistent profile scoped to the Canvas account identity and site origin, and
open an interactive browser for the user's own sign-in. Unknown origins and
non-HTTPS requests are blocked. Sign-in POSTs are limited to recognized paths;
each requires separate one-time user approval and expires after 60 seconds.
Requests are routed through a public-address DNS-pinned transport, with bounded
response size; redirects are intercepted and must stay within approved origins. WebSockets and service workers are
blocked. Unsupported cookie syntax and other network requests fail closed.

Checking the requested page is a separate action: it makes a single GET for the
exact source URL with page scripts, subresources and redirects disabled. It
shows bounded text only in the local UI. A user must confirm it is the requested
course page; a generic sign-in or enrollment chooser is not proof of course
access. Failure or expiry does not alter saved source data or tracker state.
The current slice does **not** send authenticated page content to the Assistant,
create adapters from it, save it to the tracker, or automatically refresh it.
The existing Analyze link workflow remains public-only. A separate explicit
consent and data-review design would be needed before using a model on
potentially private authenticated page text.

Limitations requiring real-world validation include multi-cookie and SSO flows,
login POST paths not recognized by the conservative policy, sites requiring a
Referrer header or script-rendered course pages, native HTTP Basic sign-in,
CAPTCHAs, and cross-origin assets. Such sites may remain unverified even when
the user's credentials are valid. Credentials and authentication redirects are
never printed by this module. The previous Piazza anonymous observation remains
an ambiguous login/choose-class shell, not proof that login grants course access.

`python -m canvas_hub.services.login_detector URL` makes real anonymous public
network requests. Fixture tests use synthetic responses, and the separate
screenshot research is not integrated into this branch. No live screenshot
inference or paid model call was performed here.
