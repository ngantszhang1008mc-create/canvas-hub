"""Cross-platform command construction for helpers launched as subprocesses.

Windows package managers commonly expose command-line tools as ``.cmd``
shims. ``CreateProcess`` cannot run those directly, so route only batch-file
commands through ``cmd.exe`` while leaving normal executables untouched.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path


def command_for_process(args: Sequence[str | Path]) -> str | list[str]:
    """Return a subprocess argument list that can launch ``args`` on this OS."""
    values = [str(arg) for arg in args]
    if os.name != "nt" or not values or Path(values[0]).suffix.lower() not in {".bat", ".cmd"}:
        return values
    comspec = os.environ.get("COMSPEC") or "cmd.exe"
    # cmd requires an extra pair of quotes around the complete command string
    # when both the batch shim and a later argument can contain spaces. Passing
    # a ready-made command-line string avoids the CRT re-quoting those quotes.
    inner = subprocess.list2cmdline(values)
    return f'{subprocess.list2cmdline([comspec])} /d /s /c "{inner}"'
