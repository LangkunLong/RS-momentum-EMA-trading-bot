"""Shell-safe rendering for authenticated schema-v3 optimizer commands."""

from __future__ import annotations

from pathlib import Path
import stat
import subprocess
import sys
from typing import Sequence


_MAX_V3_COMMAND_ARGUMENTS = 128
_MAX_V3_COMMAND_CHARACTERS = 8_191
_DISALLOWED_CMD_CHARACTERS = frozenset('\x00\r\n"&|<>^()%!')


def authenticated_python_executable() -> str:
    """Return the absolute, regular, non-link interpreter backing this process."""

    raw = sys.executable
    if type(raw) is not str or not raw:
        raise ValueError("schema-v3 Python interpreter is invalid")
    path = Path(raw)
    try:
        status = path.lstat()
    except (OSError, ValueError) as exc:
        raise ValueError("schema-v3 Python interpreter is unavailable") from exc
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not stat.S_ISREG(status.st_mode)
    ):
        raise ValueError(
            "schema-v3 Python interpreter must be an absolute regular non-link file"
        )
    return str(path)


def render_pit_optimizer_v3_command(argv: Sequence[str]) -> str:
    """Render one bounded argv that is safe to pass as text to ``cmd.exe /c``."""

    if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence):
        raise ValueError("schema-v3 command argv is invalid")
    arguments = tuple(argv)
    if not 1 <= len(arguments) <= _MAX_V3_COMMAND_ARGUMENTS:
        raise ValueError("schema-v3 command argv is outside its bound")
    for argument in arguments:
        if type(argument) is not str:
            raise ValueError("schema-v3 command arguments must be exact strings")
        if any(character in _DISALLOWED_CMD_CHARACTERS for character in argument):
            raise ValueError("schema-v3 command argument is unsafe for cmd.exe")
    if not Path(arguments[0]).is_absolute():
        raise ValueError("schema-v3 command executable must be absolute")
    if sum(len(argument) for argument in arguments) > _MAX_V3_COMMAND_CHARACTERS:
        raise ValueError("schema-v3 command argv is outside its bound")
    rendered = subprocess.list2cmdline(arguments)
    if len(rendered) > _MAX_V3_COMMAND_CHARACTERS:
        raise ValueError("schema-v3 rendered command is outside its bound")
    return rendered
