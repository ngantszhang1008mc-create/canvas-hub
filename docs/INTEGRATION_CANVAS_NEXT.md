# Combined Canvas Hub test version

This test branch combines external-site connections, tracker/layout controls,
Assistant context and guarded tracker updates, and the optional allowance
gateway. It is not a deployment or a live free-AI service. Existing BYOK and Codex
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
Ask the Assistant to list trackers. The local mock calls `list_components` and
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
- Manual context compaction uses the selected provider and consumes its quota.
  Original history is retained. Recorded usage is not a complete billing ledger:
  ordinary failed/cancelled requests may have consumed quota without a saved Turn.

The context schema migration is `0017`; the other three features add no local
SQLite migration. For any later real-data upgrade, stop the app and back up the
whole private data home consistently before startup. Restore the matching backup
for rollback; do not run older code against a newly migrated database.

## Integration checkpoint validation

The gateway client/settings/context/source flow passed 15 focused Python tests,
including real Chromium at 1280px and 380px (19.17 seconds). A separate upgrade
case passed from schema 16 to 17, preserving manual tracker state and original
chat messages; fresh test homes also start at 17. TypeScript checking and seven
focused gateway tests passed (6.12 seconds), using mocked OpenAI HTTP only.
The initial narrow-screen settings overflow was fixed and rechecked.

A real local Wrangler/SQLite runtime also completed invitation enrollment,
allowance reads, an Assistant `list_components` tool round and the next response:
two settled mock requests, no remaining hold. The synthetic preview launcher
started successfully. No full suite or real model/login test was run at this
checkpoint. The next tool-picker/context-default and token-optimization inputs
are still pending; final combined validation and the user test invitation wait
for those exact heads.
