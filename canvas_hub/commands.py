"""Cross-platform command construction for helpers launched as subprocesses.

Windows package managers commonly expose command-line tools as ``.cmd``
shims. ``CreateProcess`` cannot run those directly, so route only batch-file
commands through ``cmd.exe`` while leaving normal executables untouched.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class PreparedCommand:
    command: str | list[str]
    env: dict[str, str] = field(default_factory=dict)


def command_for_process(args: Sequence[str | Path]) -> PreparedCommand:
    """Prepare exact argv passing, routing Windows batch shims through cmd."""
    values = [str(arg) for arg in args]
    if os.name != "nt" or not values or Path(values[0]).suffix.lower() not in {".bat", ".cmd"}:
        return PreparedCommand(values)
    comspec = os.environ.get("COMSPEC") or "cmd.exe"
    # cmd expands metacharacters such as & and percent variables while parsing.
    # Put each value in a private environment variable and expand it once inside
    # quotes. Variable contents are not re-parsed for those shell characters.
    env, tokens = {}, []
    for index, value in enumerate(values):
        name = f"CANVAS_HUB_ARG_{index}"
        env[name] = value
        tokens.append(f'"%{name}%"')
    inner = " ".join(tokens)
    return PreparedCommand(
        f'{subprocess.list2cmdline([comspec])} /d /v:off /s /c "{inner}"',
        env,
    )


def _environment(prepared: PreparedCommand, supplied: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    if supplied:
        env.update(supplied)
    env.update(prepared.env)
    return env


def run_command(args: Sequence[str | Path], **kwargs):
    """``subprocess.run`` with shell-safe exact argument passing."""
    prepared = command_for_process(args)
    kwargs["env"] = _environment(prepared, kwargs.get("env"))
    return subprocess.run(prepared.command, **kwargs)


def popen_command(args: Sequence[str | Path], **kwargs):
    """``subprocess.Popen`` with shell-safe exact argument passing."""
    prepared = command_for_process(args)
    kwargs["env"] = _environment(prepared, kwargs.get("env"))
    return subprocess.Popen(prepared.command, **kwargs)
