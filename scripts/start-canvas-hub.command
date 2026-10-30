#!/bin/bash
# Double-click on macOS (or run from Terminal) to start Canvas Hub.
# First run installs Python dependencies and the Playwright Chromium browser.
cd "$(dirname "$0")/.." || exit 1
if ! command -v uv >/dev/null 2>&1; then
  echo "Canvas Hub needs 'uv' (a Python installer). Install it with:"
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo "then open a new Terminal window and run this script again."
  read -r -p "Press Return to close." _
  exit 1
fi
uv sync --quiet || exit 1
if [ -z "$CANVAS_HUB_BROWSER_CHANNEL" ]; then
  uv run playwright install chromium || exit 1
fi
exec uv run canvas-hub serve "$@"
