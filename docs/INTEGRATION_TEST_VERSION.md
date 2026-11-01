# Integrated test version

`codex/integration-study-hub` combines these verified feature heads for testing:

| Feature | PR | Source commit |
| --- | --- | --- |
| Study workspaces and source-bound tools | #23 | `3247099c8eed767335434582a3efaab98e75bce4` |
| External course sources | #24 | `0d9cf24328c0f6247a45d50ccf7caf71899a3cd7` |
| Interactive Overall widgets | #25 | `39b0d21aea3c8a7edce52b98899f98e5f034fce2` |

This is a separate test branch. No feature PR or `main` is merged or deployed by
this workflow. The adapter branch includes its schema-assertion compatibility fix.

## Integration fixes and ownership

- **Widget/external-source bridge:** an explicit date-window or active-work rule
  referencing an external item now checks whether its adapter is available and
  the record appeared in the last successful read. Recent old timestamps alone
  must not make a failed/missing source look current. An uncertain conditional
  card moves to review; pinned cards remain usable. This requires both the
  `ExternalSources.item_issue` helper and the widget service hook, so carry the
  bridge together when integrating #24 and #25.
- **External-source lifecycle (#24 follow-up):** proposals, access errors and
  disable/decline decisions now synchronize the existing component source
  status after the source transaction. Previously only adapter reads did so;
  pinned tools could retain an obsolete "Source linked" status while their
  adapter was paused. Saved values, overrides and checkbox progress are retained.

The feature branches themselves are unchanged. New regression tests and fixture
demo files belong to this integration branch. No new migration is needed.

## Reproduce the synthetic preview

From this branch's source checkout:

```sh
uv sync --locked --python 3.13
uv run python scripts/preview-integration.py --port 8734 --metadata /tmp/study-hub-integration-preview.json
```

The metadata file (mode 0600) contains the private launch URL. Open it once to
establish the local authentication cookie, then visit `/testing/demo` for the
fixture controls. Do not publish the metadata or token. The server listens on
loopback only, using a new temporary home; it does not touch an existing demo.
Optionally pass `--home /path/to/a/new-synthetic-home` to retain this run's data.
An existing path is refused. Stop the process when finished; the default
temporary home is then removed. Back up any separate existing preview before
switching its process/code. This launcher never imports into that preview.

The seed records an explicit synthetic adapter proposal and approval, then binds
the existing HMAW calculator and a checklist to normalized fixture assignments.
The calculator uses a documented user-chosen date window with the external item
as evidence. Daily and seminar checklists retain their original state semantics.

The source transport accepts only one fixed `.example.edu` fixture URL and
returns local HTML; every other URL fails without a network fallback. It is
injected only by the test/demo code. The Assistant uses a local fake responder,
and no Canvas collector is installed in the preview process. Test controls are
not registered in the normal application. Their API uses the normal token,
origin and `X-Canvas-Hub` protections.

Try this sequence:

1. Tick the reflection checkbox and adjust its calculator slider on Overall.
2. In `/testing/demo`, select the revised fixture, then read it. Return to
   Overall: the title, deadline and maximum points change while your inputs stay.
3. Select temporary failure and read again. The conditional calculator needs
   review; the pinned checklist shows unavailable evidence and keeps progress.
4. Select the revised fixture and read again to recover. A user-hidden widget
   stays hidden; a pinned widget stays pinned.
5. For a template-change failure, select a valid fixture and prepare a synthetic
   adapter proposal. Review and approve its exact revision in the HMAW course's
   Source Agent panel. Source approval does not approve widget placement or a
   pending component definition.

No daily recurrence/reset or automatic AI repair is introduced. Home condition
evaluation still happens on opening/refresh/settings changes; an open page does
not poll source pages. External adapters still support only reviewed public
declarative reads, with the limits documented in `EXTERNAL_SOURCES.md`.

## Verification

```sh
uv run pytest -q
uv run pytest -q tests/test_integration_study_hub.py tests/test_integration_study_hub_ui.py
```

Install the Playwright Chromium browser first, or set `CANVAS_HUB_CHROMIUM` to a
system Chromium executable. Set `CANVAS_HUB_INTEGRATION_SCREENSHOTS` to retain
the browser evidence. The new tests cover the full source-to-widget path,
normalized updates, retained overrides and undo, account/course boundaries,
independent approval namespaces, contextual run dedup/cancel, no-LLM refresh,
and combined Agent panels/launchers at 1280px and touch 380px in Light/Dark.

Migration tests use the actual SQL files to upgrade stores from schema 8
(pre-#23), schema 12, schema 13 (adapters first), and schema 14 with 13 absent
(widgets first). The runner records individual applied migrations, not just
the largest version: 0013 can be applied after 0014. Existing component JSON,
version/undo history, tracker pins, source registrations and hidden placements
survive. No bulk component imports, resets or account remapping occur.
