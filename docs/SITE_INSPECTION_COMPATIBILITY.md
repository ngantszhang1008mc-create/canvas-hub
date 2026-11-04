# Isolated site inspection compatibility

This draft builds on PR #45 (`e5928a4`) and includes main through PRs #46 and
#47 (`c1a63d9`). External inspection remains disabled by the shipping freeze.
No migration rewrites saved snapshots, and no real authenticated login or live
Assistant classification was tested for this change.

## Network rules and evidence

Anonymous, proxy-respecting GETs on 2026-11-04 observed:

- [Zinc](https://zinc.cse.ust.hk/) redirects to
  `https://api.zinc.cse.ust.hk/auth/login`, then to Microsoft authorization.
  Its login HTML declares scripts/styles on `https://aadcdn.msauth.net` under
  `/shared/1.0/content/` and `/ests/2.1/content/`. Its bootstrap declares
  `/{tenant}/login` and `/common/GetCredentialType`.
- [Microsoft's login endpoint](https://login.microsoftonline.com/common/login)
  also declares assets on that exact static host. This GET returned an error
  page, and is evidence of asset locations only.
- [Piazza home](https://piazza.com/home) declares scripts on
  `https://dvngeac8rg9mb.cloudfront.net/javascripts/` and a `/slider_login` form.
  The publicly referenced [join script](https://piazza.com/js/splash/join.js)
  invokes `user.login`; the [AJAX client](https://piazza.com/piazzza_js/ajax.js)
  sends JSON to `/logic/api?method=user.login&aid=…`. That API also carries
  unrelated operations and telemetry, so a host/path allowlist alone is unsafe.

These requests used no account credentials, submitted no forms, and did not run
the application transport against live sites. Raw bootstrap HTML, auth queries,
cookies and generated login state are not committed. Oxygen was not independently
probed; its reported Microsoft sign-in may benefit from the same rules, but
compatibility is not established.

`site_policy.py` retains exact URLs declared by the original server HTML,
including Microsoft's JSON bootstrap, for the currently loaded document. CDN
rules also require the exact source origin, static host, allowed path prefix,
script/style resource type, query-free URL, and matching response MIME type.
No wildcard CDN domains, CSS fonts/images, arbitrary lazy URLs or CDN redirects
are allowed. Even an explicitly approved CDN origin receives no cookies,
authorization, referrer, Origin or CSRF headers through the static-resource
exception; its responses cannot set cookies. The anonymous detector uses the
same static manifest and a 32-request cap within its existing subprocess deadline.
It still makes no POSTs and never concludes course access from a login screen.

Interactive POST candidates are limited to credential-bearing forms declared by
the fetched page, the two declared Microsoft actions above, and Piazza's exact
JSON login operation. Piazza JSON must contain only `method` and `params`, with
only `email` and `pass` parameters. Mixed telemetry (`logdata`), other API methods,
unknown authentication routes, recovery, MFA endpoints without reviewed evidence,
and generic path-keyword matches remain blocked. Existing mutation-URL checks
still apply. Adding a domain does not approve its POSTs or arbitrary cross-origin
resources.

The browser holds one candidate request for up to 60 seconds. A one-time local
approval releases that original request; it does not replay a failed submission.
The approval binds the initiating document, full destination, method/type and
body through a process-local keyed digest. Navigation, replacement, cancellation
and expiry discard the pending request. The body remains only in browser memory
while pending; neither its bytes nor digest enter status, logs or storage. The
browser window may show a pending request until it is approved or closed.
Unknown or changing flows fail closed instead of gaining a general POST grant.

All requests still use the bounded, DNS-pinned, public-address transport, verified
TLS, destination-scoped cookies and controlled redirect navigation. POST 307/308
replay, private DNS, popups, embedded documents, WebSockets and service workers
remain blocked. Basic credentials still authorize only the challenged exact GET.

## Diagnostics, closure and saved text

Connection diagnostics retain at most 24 grouped decisions in memory: origin,
method, resource type, fixed endpoint label/template, reason, count and optional
HTTP status. Unknown paths are withheld. There are no request bodies, headers,
query strings, fragments, raw exception messages or credential hashes. The local
UI also exposes bounded anonymous-probe diagnostics. Labels distinguish a
recognized request awaiting approval from an unverified submission, a blocked
cross-origin resource and a rejected CDN response.

Cancel marks the connection `closing` immediately. It returns a terminal state
only after browser cleanup, credential/queue cleanup and thread completion.
Timed-out closes remain closing until cleanup finishes. Profile reuse and the
connection count check actual thread lifetime, and the UI withholds reopen
controls during shutdown.

Text extraction folds ordinary HTML whitespace before applying its source limit,
preserves preformatted text across nested spans, removes blank/trailing padding,
and retains code indentation. Both the DOM/node budget and the final 100,000
character storage/provider budget append an explicit source-truncation marker.
Existing truncated snapshots cannot recover discarded text; a newly authorized
inspection is required to replace them. No existing COMP1023 snapshot was read,
rewritten, or represented as recovered.

## Dynamic extraction assessment

A realistic synthetic page rendered course content with JavaScript during
interactive browsing. Static inspection then issued only its exact document GET
with JavaScript, resources and redirects disabled, and returned no course text.
The UI now identifies that insufficient static content may require JavaScript;
it does not treat this result as verified access or a login failure.

Some dynamic sites therefore need a separate extraction strategy. A prospective
mode should freeze an already rendered, exact-source top-level document, stop
network traffic and script execution before reading, exclude controls and hidden
content, bind review to the account/source/session/digest, expire the frozen
snapshot, and describe its freshness accurately. It must not silently enable
scripts or general authenticated API POSTs during inspection. SPA routes,
post-login redirects, session expiry and cross-origin APIs need independent
evidence before adopting such a mode. This draft does not implement or claim
live compatibility for it; the existing reviewed/Basic inspection contracts stay
intact.

## Verification

Fixtures in `tests/fixtures/site_inspection/` are authored synthetic pages modeled
on the anonymous markup shapes above. They use fake transports and credentials,
real system Chromium, and no provider. The focused regression command and final
results are recorded in the draft PR. No broad local suite, deployment, paid
request, shipping enablement or merge was performed.
