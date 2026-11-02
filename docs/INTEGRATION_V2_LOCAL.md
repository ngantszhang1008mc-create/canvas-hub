# Local integration v2 — not published or installed

This local integration combines the original `eff9eb578869fc475722215c17651486a9cef096`
test baseline with these exact public feature commits:

| Feature | Commit | Tree |
| --- | --- | --- |
| PR #26: built-in analysis, Analyse all, reminders | `f3ecc7c92d9115f5a55bc916210a5c238f2ddac4` | `af275eb8c0bc5d86112f5b8d4b2de115628752ca` |
| PR #27: background collection pipeline | `9f5c74cdbcf1bb4ad0c02c99af09e3d56f96f5ef` | `a713d9bd7292a12b3828a66f31966c2544baf923` |

PR #27 was first verified at `c9aa21d6d89119a6920a58598c9b925b101fa9df`, tree
`08401a41dfc0facacc5bc27d5bf70a3186e61627`. Its confirmed follow-up changes only
the collection-progress test screenshot path to pytest's temporary directory.
The final regression includes the follow-up. Feature branches and GitHub PRs
remain untouched; `main` is neither changed nor deployed.

## Additional local fixes — a plain merge is insufficient

The integration adds an outer-transaction `Database.after_commit` hook queue.
The importer still calls `after_discovery`, `after_changed_import` and the legacy
`after_import`, in that order. Normal imports call them synchronously as before;
imports nested inside adapter publication defer them until the **outermost**
commit. A rollback discards its callbacks. This retains atomic adapter publication
while ensuring component locks are acquired after releasing the SQLite write
lock. The durable component refresh outbox remains committed with observations,
so failed refreshes can still be replayed. There is no extra migration for this fix.

The pipeline migration test now uses the real source-analysis migration rather
than inserting a second synthetic version 15. Additional integration tests cover
real 15/16 migration order, hook ordering/rollback, slow downloads and extraction
alongside built-in batches, supplemental source/widget refresh, search, manual
state, account isolation and independent cancellation. Real-browser cross-feature
tests run at 1280px and touch 380px using local pages and fake providers.

**The locally verified tree includes these additional fixes.** Merging only the
two public feature refs does not reproduce it. The parent must choose an authorized
way to deliver the local integration fix patch (or publish an approved integration
ref) before upgrading a user's installation. Compare the delivered patch SHA-256
and final tree with the separate handoff manifest. Never substitute a newer moving
feature head for a pinned, tested commit.

## Reproduce the code without touching a running test installation

In a clean local staging checkout (not the user's active test checkout):

```sh
git fetch origin codex/builtin-source-analysis codex/collection-pipeline
git cat-file -e f3ecc7c92d9115f5a55bc916210a5c238f2ddac4^{commit}
git cat-file -e 9f5c74cdbcf1bb4ad0c02c99af09e3d56f96f5ef^{commit}
git switch -c local/integration-v2-review eff9eb578869fc475722215c17651486a9cef096
git merge --no-ff f3ecc7c92d9115f5a55bc916210a5c238f2ddac4
git merge --no-ff 9f5c74cdbcf1bb4ad0c02c99af09e3d56f96f5ef
# After the parent has approved/delivered the integration fix patch:
git apply --check /verified/path/integration-study-hub-v2-fixes.patch
git apply --index /verified/path/integration-study-hub-v2-fixes.patch
git write-tree  # must equal the handoff's final tree before committing/installing
git commit -m 'Apply reviewed local v2 integration fixes'
```

Merge commit IDs can differ because their author/time differs; the final tree must
match. A mismatch is a stop condition, not a reason to reset or force-apply a patch.
No push or new PR is required for this local reproduction.

## Upgrade the existing Mac test checkout against the confirmed data home

This is a procedure for the authorized desktop task; no Mac operation was performed
by the integration task. The parent has now verified the clean test checkout
`~/canvas-hub-test-eff9eb5` at `eff9eb5` and the actual schema-14 data home
`~/Library/Application Support/CanvasHub`, with no active process/job. The user
explicitly authorized the test checkout to use that original data home. It is
**shared existing data, not a disposable preview home**. The previously considered
`CanvasHub-Test-eff9eb5` directory is not the selected data home. Recheck the process,
home and checkout immediately before the desktop upgrade; a verified backup and
matching rollback remain mandatory. Publication of the integration branch is a
separate approval step; do not install a moving or unvalidated ref.

1. Record the verified checkout, original commit/branch, test home, port and how
   its server is supervised. Do not copy a launch URL token, API key or cookies
   into chat, logs or a PR. In the checkout run `git status --short
   --untracked-files=all`. Any tracked edits or untracked files require preservation
   and review; stop rather than `reset --hard`, `clean -fd`, force checkout or
   overwriting unknown files. Confirm the approved v2 code is available and its
   tree matches before stopping a working installation.
2. Pause/cancel active collection and source batches through their UI, then stop
   the test server normally (Ctrl-C or its known supervisor). Stop any MCP/other
   process writing **that same home** as well. Let requests drain and wait for
   process exit. Check the exact PID/port and `lsof` on the verified database,
   `-wal` and `-shm` files; no writer may remain. Do not force-kill a process just
   to make a copy appear complete. Do not restart during backup.
3. Make a new private, local snapshot of the **entire verified test home**, after
   the stop. This preserves resources, tracker/manual state, API setup and browser
   profiles without reading their secrets. Copy the database, WAL and SHM together
   if the sidecars exist. Never copy only the main SQLite file while ignoring WAL,
   and never delete WAL/SHM to force a checkpoint. Do not upload this snapshot.

Example backup commands, only after filling in verified paths and completing the
stop checks above (all variables are task-specific):

```sh
test_checkout="$HOME/canvas-hub-test-eff9eb5"
test_home="$HOME/Library/Application Support/CanvasHub"
backup_parent='/verified/private/local/backup/directory'
umask 077
backup_dir=$(mktemp -d "$backup_parent/canvas-test-pre-v2.XXXXXX")
git -C "$test_checkout" rev-parse HEAD > "$backup_dir/code-head.txt"
git -C "$test_checkout" branch --show-current > "$backup_dir/code-branch.txt"
ditto "$test_home" "$backup_dir/home"
CANVAS_TEST_SNAPSHOT="$backup_dir" "$test_checkout/.venv/bin/python" - <<'PY'
import os, sqlite3
from pathlib import Path
path = Path(os.environ['CANVAS_TEST_SNAPSHOT']) / 'home/data/canvas-hub.sqlite3'
assert path.is_file(), 'Wrong home: no database in snapshot'
conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
try:
    assert conn.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    print('Stopped-home SQLite snapshot verified')
finally:
    conn.close()
PY
```

Use standard `sqlite3` for verification, not `Hub.open`, which would migrate the
backup. Keep the snapshot outside the home being upgraded. Confirm the original
code commit remains available for rollback. The snapshot contains secrets and
must remain private; verification prints no account content or setting values.

4. With a clean working tree, update the **existing test checkout** to the exact
   approved local/published integration commit using `git merge --ff-only
   <verified-integration-commit>`. The old `eff9eb5` baseline is an ancestor. If
   fast-forward is refused, stop and reconcile history without discarding local
   work. Do not silently substitute `main` or a moving feature branch. Run
   `uv sync --locked --python 3.13` in that checkout.
5. Start the existing test service using the same verified home/port, e.g.
   `uv run canvas-hub --home "$test_home" serve --port <verified-test-port>`.
   The app migrates the stopped database; it must retain account mappings,
   browser/profile directories, Assistant setup, widget choices, overrides and
   checkbox progress. Do not run the synthetic preview launcher against this
   existing home. Opening the new UI does not approve paid source analysis;
   the user remains responsible for any explicit real-provider consent.
6. Verify schema 16, preserved manual state, the scoped Analyse all/reminder UI,
   truthful collection phase progress and Experimental concurrency off unless
   the user had explicitly enabled it. Inspect old interrupted/paused work before
   choosing resume; do not automatically retry cancelled source analysis. Keep
   the snapshot until the user accepts the upgrade.

## Rollback is code plus matching data

Stop all writers normally again. Preserve the post-upgrade home in a **new** local
quarantine directory (rename/copy, no deletion), including any new manual work.
With a clean checkout, select the recorded pre-upgrade commit using a non-forcing
`git switch --detach <recorded-code-head>` (or the preserved branch), and reinstall
its locked dependencies. Restore the complete stopped-home snapshot to the exact
original test-home path, including its matching DB/WAL/SHM, resources, settings
and profiles. Validate that restored copy with standard SQLite before restarting
the old code with the original home/port. Do not run old code against schema 16
as a substitute for restoring the matching backup. New edits remain in quarantine
for deliberate recovery; never silently overwrite them or the snapshot.

## Validation commands

```sh
CANVAS_HUB_CHROMIUM=/usr/bin/chromium uv run pytest -q -rs
uv run pytest tests/test_integration_study_hub_v2.py -q
uv run pytest tests/test_integration_study_hub_v2_ui.py -q
```

The Chromium override above is for the cloud Linux executor only. On macOS use
`uv run playwright install chromium` and omit that override. Tests use independent
temporary homes, local fixture pages and fake providers, never the existing test
home. Migration cases cover empty DB, schema 14 (`eff9eb5`), only 15, and only 16
(with 15 applied later). Existing accounts, source decisions, manual component
state, widget placements and pending dependency changes survive the upgrade.
