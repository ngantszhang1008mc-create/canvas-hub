"""Platform-specific launch and storage behavior."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from canvas_hub.commands import command_for_process
from canvas_hub.paths import default_home
from canvas_hub.web.app import _mcp_candidates


def test_windows_default_home_uses_local_app_data(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.delenv("CANVAS_HUB_HOME", raising=False)
    assert default_home() == tmp_path / "Local" / "CanvasHub"


def test_windows_default_home_falls_back_to_local_app_data_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("CANVAS_HUB_HOME", raising=False)
    assert default_home() == tmp_path / "AppData" / "Local" / "CanvasHub"


def test_home_override_still_wins(tmp_path, monkeypatch):
    override = tmp_path / "custom"
    monkeypatch.setenv("CANVAS_HUB_HOME", str(override))
    assert default_home() == override.resolve()


def test_mcp_console_script_candidates_are_platform_specific(tmp_path):
    assert [p.name for p in _mcp_candidates(tmp_path, "nt")] == [
        "canvas-hub.exe", "canvas-hub.cmd", "canvas-hub.bat"
    ]
    assert [p.name for p in _mcp_candidates(tmp_path, "posix")] == ["canvas-hub"]


@pytest.mark.skipif(os.name != "nt", reason="Windows batch-file launch")
def test_windows_batch_shim_is_routed_through_cmd(tmp_path):
    shim = tmp_path / "hello.cmd"
    shim.write_text("@echo off\r\necho hello\r\n", encoding="utf-8")
    result = subprocess.run(command_for_process([shim]), capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "hello"
