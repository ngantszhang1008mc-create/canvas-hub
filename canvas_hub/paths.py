"""Application data locations.

Course data (database, resources) and browser sessions live in *separate*
directory trees so that exports/backups can never pick up browser state:

    <home>/data/canvas-hub.sqlite3       durable store
    <home>/data/resources/originals/..   downloaded originals (content addressed)
    <home>/data/resources/extracted/..   extracted text
    <home>/data/backups/                 default backup destination
    <home>/browser-profiles/<instance>/  app-managed Chromium profiles (sessions)
    <home>/runtime/                      local API token, lock files, logs

<home> defaults to ~/Library/Application Support/CanvasHub on macOS and
$XDG_DATA_HOME/canvas-hub (or ~/.local/share/canvas-hub) elsewhere. It can be
overridden with CANVAS_HUB_HOME.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def default_home() -> Path:
    env = os.environ.get("CANVAS_HUB_HOME")
    if env:
        return Path(env).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "CanvasHub"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "canvas-hub"


@dataclass(frozen=True)
class AppPaths:
    home: Path

    @property
    def data(self) -> Path:
        return self.home / "data"

    @property
    def db(self) -> Path:
        return self.data / "canvas-hub.sqlite3"

    @property
    def resources(self) -> Path:
        return self.data / "resources"

    @property
    def originals(self) -> Path:
        return self.resources / "originals"

    @property
    def extracted(self) -> Path:
        return self.resources / "extracted"

    @property
    def backups(self) -> Path:
        return self.data / "backups"

    @property
    def browser_profiles(self) -> Path:
        return self.home / "browser-profiles"

    @property
    def runtime(self) -> Path:
        return self.home / "runtime"

    def ensure(self) -> "AppPaths":
        for p in (self.data, self.originals, self.extracted, self.backups, self.browser_profiles, self.runtime):
            p.mkdir(parents=True, exist_ok=True)
        for p in (self.home, self.browser_profiles, self.runtime):
            try:
                os.chmod(p, 0o700)
            except OSError:
                pass
        return self

    def profile_dir(self, instance_id: int) -> Path:
        return self.browser_profiles / f"instance-{int(instance_id)}"


def get_paths(home: Path | None = None) -> AppPaths:
    return AppPaths(Path(home).resolve() if home else default_home()).ensure()
