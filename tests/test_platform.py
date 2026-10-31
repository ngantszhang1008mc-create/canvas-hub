"""Platform-specific launch and storage behavior."""

import json
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


def test_macos_default_home_uses_application_support(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("CANVAS_HUB_HOME", raising=False)
    assert default_home() == tmp_path / "Library" / "Application Support" / "CanvasHub"


def test_mcp_console_script_candidates_are_platform_specific(tmp_path):
    assert [p.name for p in _mcp_candidates(tmp_path, "nt")] == [
        "canvas-hub.exe", "canvas-hub.cmd", "canvas-hub.bat"
    ]
    assert [p.name for p in _mcp_candidates(tmp_path, "posix")] == ["canvas-hub"]


@pytest.mark.skipif(os.name != "nt", reason="Windows batch-file launch")
def test_windows_batch_shim_preserves_spaced_and_unicode_arguments(tmp_path):
    program_dir = tmp_path / "Program Files" / "OCR 工具"
    program_dir.mkdir(parents=True)
    script = program_dir / "argv.py"
    script.write_text(
        "import json, sys\n"
        "sys.stdout.buffer.write(json.dumps(sys.argv[1:], ensure_ascii=False).encode('utf-8'))\n",
        encoding="utf-8",
    )
    shim = program_dir / "tesseract shim.cmd"
    shim.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    image = tmp_path / "Jane Doe" / "扫描 image.png"
    image.parent.mkdir()
    image.write_bytes(b"not really an image")

    command = command_for_process([shim, image])
    assert isinstance(command, str)
    result = subprocess.run(command, capture_output=True, check=True)
    assert json.loads(result.stdout.decode("utf-8")) == [str(image)]


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS argument passing")
def test_macos_subprocess_arguments_remain_direct_and_lossless(tmp_path):
    script = tmp_path / "helpers with spaces" / "接收参数.py"
    script.parent.mkdir()
    script.write_text(
        "import json, sys\n"
        "sys.stdout.buffer.write(json.dumps(sys.argv[1:], ensure_ascii=False).encode('utf-8'))\n",
        encoding="utf-8",
    )
    image = tmp_path / "Jane Doe" / "扫描 image.png"
    image.parent.mkdir()
    image.write_bytes(b"data")

    command = command_for_process([sys.executable, script, image])
    assert command == [sys.executable, str(script), str(image)]
    result = subprocess.run(command, capture_output=True, check=True)
    assert json.loads(result.stdout.decode("utf-8")) == [str(image)]


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS launcher")
def test_macos_launcher_remains_executable_and_valid():
    launcher = Path(__file__).parents[1] / "scripts" / "start-canvas-hub.command"
    assert os.access(launcher, os.X_OK)
    subprocess.run(["bash", "-n", launcher], check=True)
    assert "uv run canvas-hub serve" in launcher.read_text(encoding="utf-8")
