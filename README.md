# Canvas Hub

A personal application for collecting, organizing, searching, and tracking changes in Canvas course information.

The app should support a user-provided Canvas URL and interactive institutional login without requiring a Canvas API token. The initial target is a local application on macOS, with durable storage, source links, history, and explicit collection coverage.

## Implementation brief

[Read the complete brief for Claude](docs/CLAUDE_BRIEF.md).

Claude builds the main application and owns all UI/UX design. The actual Codex integration is reserved for Codex after the main application works; Claude should provide documented service boundaries and a `CODEX_HANDOFF.md`.

## Status

Planning repository initialized. Application implementation has not started.

Keep credentials, browser sessions, downloaded private course materials, and runtime databases out of this repository. The main application must work independently of the future Codex module.
