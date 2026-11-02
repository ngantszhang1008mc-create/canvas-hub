# External login research checkpoint

This branch is experimental and is not a connected-site implementation. Do not
install it as a login solution. The anonymous `tools/login_probe.py` is based on
the separate `research/login-detection` prototype; it runs a fresh browser and
does not reuse a user's profile. `python -m tools.login_probe URL` performs real
public network requests; the fixture tests do not contact real sites.

The public reader now distinguishes an HTTP authentication challenge from an
unexplained denial and a transient unsuccessful response. This is only one
part of detection: the in-app reader does not yet use the rendered probe, and
an existing simple HTML password-form heuristic may still misclassify pages
that contain both substantive content and an optional login form. External MCP
Agent access errors are still treated using their existing semantics.

Before a connected-site implementation is usable, it needs explicit per-site
consent, local profiles scoped to both account and site, a user-driven SSO flow
with an independently approved identity-provider scope, protections against
private-network and cross-origin requests, verification of the actual requested
course page, bounded read-only retrieval, session expiry and cancellation, and
end-to-end synthetic-site tests. The existing public reader deliberately does
not send cookies or reuse profiles. No connected-session UI or background
crawling is enabled by this checkpoint.

The previous Piazza anonymous observation was an ambiguous login/choose-class
shell; it did not establish whether login grants course access. No additional
real screenshot inference or provider request was performed on this branch.
