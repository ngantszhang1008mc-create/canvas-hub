# Combined Canvas Hub test version

This test branch combines external-site connections, tracker/layout controls,
Assistant context and guarded tracker updates, and the optional allowance
gateway. It also includes `/tracker` and the searchable tool picker, transient
source context, and normalized usage/cache counters. Automatic compaction is
off by default; manual compaction and original history remain available.
It is not a deployment or a live free-AI service. Existing BYOK and Codex
setups remain available. Real Oxygen/Zinc login compatibility is unverified.

## Offline app and gateway

Use a checkout of the integration branch, Python 3.11+, uv, and a current Node.js
version supported by Wrangler (22 or later). These commands use synthetic data
and a separate temporary app home; they do not migrate an existing account.

From the repository root:

```sh
uv sync
cd gateway
npm ci
cd ..
uv run python gateway/scripts/local_mock.py
```

Start the local gateway in a separate terminal, from `gateway/`:

```sh
npx wrangler dev --local --ip 127.0.0.1 --port 8799 --config work/app-mock/wrangler.json --persist-to work/app-mock/state
```

Then from the repository root:

```sh
uv run python gateway/scripts/local_mock.py --invite
uv run python scripts/preview-integration.py --port 8734 --metadata gateway/work/app-mock/preview.json
```

Open the private `url` in `gateway/work/app-mock/preview.json`. In **Assistant →
Settings**, add an **Allowance gateway** setup with URL `http://127.0.0.1:8799`,
mode **Mock**, and model `mock-text`. Review the disclosure, paste the one-time
invitation from the file printed by the helper, then choose **Connect with
invitation** and activate this setup. **Check allowance** makes no model request.
Choose `/list_components` in the composer, add a question asking to list trackers,
and send. Selecting a tool alone runs nothing. The local mock calls `list_components` and
then returns a fixed answer after receiving its result.

The mock is a protocol fixture, not an intelligent model: it cannot classify real
pages or write useful summaries. Browser tests use scripted synthetic responses
to exercise those paths. The preview includes synthetic trackers/source controls
and never logs into Canvas. Stop both terminals with Ctrl+C when finished.
Generated credentials, invitation files and the local ledger remain in ignored
`gateway/work/app-mock/`; the helper refuses to overwrite an existing setup.
Reusing the gateway retains its synthetic quota history. A new invitation creates
a new synthetic user; production account recovery belongs to the operator.

## Connection and usage boundaries

- Public link analysis still requires explicit content/provider consent. For a
  connected website, separately approve the exact site/login origins, sign in
  manually, verify the requested page, then review the captured content before
  analysis. Credentials and Canvas browser sessions are never sent to the model.
- Approved login redirects use bounded, DNS-pinned fresh navigation. Subresource
  redirects, POST-preserving 307/308, and redirects during page verification are
  refused. Some real SSO flows therefore remain unsupported.
- Gateway remote origins require HTTPS. Session credentials stay in the local
  private Assistant store, not the browser. Each model/tool round uses a new
  reservation; cancellation or a lost terminal response may leave a hold. There
  is no automatic model retry or paid fallback. The operator must separately
  configure and fund a production service; this branch does not provide one.
- Removing a gateway setup/session, withdrawing consent, or changing its reviewed
  tools stops an active run before further dispatch or local tool execution. A
  request already in flight may still have consumed allowance. Changing the
  active setup alone does not redirect an existing run to a different provider.
- Manual context compaction uses the selected provider and consumes its quota.
  Original history is retained. New chat attempts are recorded even after
  cancellation/failure; absent provider counts stay unknown. Historical failed
  requests may be missing. Local telemetry never determines gateway allowance.

The context schema migration is `0017`; provider usage adds `0018` to distinguish
chat attempts from summaries in the existing usage table. External connections,
layout and the gateway add no local SQLite migration. For a real-data upgrade,
stop the app and back up the
whole private data home consistently before startup. Restore the matching backup
for rollback; do not run older code against a newly migrated database.

## Combined validation

All requested inputs are included on `codex/integration-canvas-next`, based on
main `846a1a690f93a77a6747b34972f34705352e08eb`:

| Input | Published source commit |
| --- | --- |
| External-site connection and safe redirects (PR34) | `2a4634299f476a01b3eecb330cdbb56d10b2681c` |
| Tracker/layout controls | `644cefacf8433b02c9ab13eee6f4ae32d381275c` |
| Allowance gateway (PR35) | `38844a79cb4ec9aadb00d260889902557a8f4f96` |
| Context and tool picker (PR36) | `c114cde67858256a601f97bfbdf21bee6cd45eb9` |
| Usage/cache normalization | `58638db62089a886b0607f197747ee0479b06d30` |
| Source payloads | `de189a2cc866e0b3b08d184e5c019259e63e488b` |

The combined run passed **42 focused Python/browser checks in 84.02 seconds**:
tool selection and discovery, defaults off, source context/usage, guarded tracker
updates, retry/cancellation, connected-content consent, redirect/DNS boundaries,
and desktop/narrow layout. Chromium covered the picker at 1180px/380px, gateway
flows at 1280px/380px, and tracker/source/action layouts at 1440px/390px/320px.
Screenshots were inspected. JavaScript syntax checks passed. Earlier TypeScript
checking and seven focused gateway mode/Responses-adapter checks passed in 6.12
seconds; that backend was unchanged by subsequent integration.

The final authorization fix adds seven focused cases: consent/session/tool/setup
revocation before dispatch, during a silent stream, and between terminal tool
responses and execution. Valid tracker, summary-failure and source/usage paths
were rechecked. These checks use local fixtures, without model usage.

Migration checks cover fresh schema 18, schema 16→18 and schema 17→18, plus
idempotent reopen. They preserve manual tracker state and original chat messages;
pre-existing usage rows remain `purpose=summary`.

A real local Wrangler/SQLite runtime completed invitation enrollment and a
Workspace source request with explicitly selected `list_components`: two settled
mock model rounds, each with one source-body copy, no source body in permanent
history, no automatic summary, usage counted twice (once per request), preserved
manual tracker state and no remaining quota hold. The synthetic preview launcher
also started successfully. No full suite, live login, real provider call,
production deployment or main-branch merge was performed.
