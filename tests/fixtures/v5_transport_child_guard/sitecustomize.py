"""Strict side-effect tripwire for nested offline transport test children."""

from __future__ import annotations

import builtins
import io
import os
from pathlib import Path
import socket
import urllib.request

_tripwire_marker = os.environ.get("V5_TRANSPORT_TRIPWIRE_MARKER")
_installed_marker = os.environ.get("V5_TRANSPORT_GUARD_INSTALLED_MARKER")


def _fail(label: str, *_args, **_kwargs):
    if _tripwire_marker:
        Path(_tripwire_marker).write_text(label + "\n", encoding="utf-8")
    raise RuntimeError("nested offline transport test side effect is forbidden")


try:
    import dotenv
except ImportError:
    dotenv = None
else:
    dotenv.load_dotenv = lambda *args, **kwargs: _fail("dotenv.load_dotenv", *args, **kwargs)

socket.create_connection = lambda *args, **kwargs: _fail("socket.create_connection", *args, **kwargs)
socket.socket.connect = lambda *args, **kwargs: _fail("socket.socket.connect", *args, **kwargs)
socket.socket.connect_ex = lambda *args, **kwargs: _fail("socket.socket.connect_ex", *args, **kwargs)
socket.getaddrinfo = lambda *args, **kwargs: _fail("socket.getaddrinfo", *args, **kwargs)
urllib.request.urlopen = lambda *args, **kwargs: _fail("urllib.request.urlopen", *args, **kwargs)

_credential_names = {
    "OPENROUTER_API_KEY",
    "OPENROUTER",
    "OPENROUTER_MANAGEMENT_KEY",
    "OPENROUTER_MANAGEMENT_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_WEBHOOK_SECRET",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "FMP_API_KEY",
    "NOTIFY_EMAIL_PASSWORD",
}
_getenv = os.getenv
_environ_type = type(os.environ)
_environ_get = _environ_type.get
_environ_getitem = _environ_type.__getitem__


def _guarded_getenv(name, default=None):
    if name in _credential_names:
        return _fail("credential getenv:" + str(name))
    return _getenv(name, default)


def _guarded_environ_get(self, name, default=None):
    if name in _credential_names:
        return _fail("credential environ.get:" + str(name))
    return _environ_get(self, name, default)


def _guarded_environ_getitem(self, name):
    if name in _credential_names:
        return _fail("credential environ[]:" + str(name))
    return _environ_getitem(self, name)


os.getenv = _guarded_getenv
_environ_type.get = _guarded_environ_get
_environ_type.__getitem__ = _guarded_environ_getitem

_open = builtins.open
_io_open = io.open


def _is_env_path(path) -> bool:
    try:
        return any(part == ".env" or part.startswith(".env.") for part in Path(str(path)).parts)
    except (TypeError, ValueError):
        return False


def _guarded_open(path, *args, **kwargs):
    if _is_env_path(path):
        return _fail(".env open")
    return _open(path, *args, **kwargs)


def _guarded_io_open(path, *args, **kwargs):
    if _is_env_path(path):
        return _fail(".env io.open")
    return _io_open(path, *args, **kwargs)


builtins.open = _guarded_open
io.open = _guarded_io_open

if _installed_marker:
    Path(_installed_marker).write_text("guard-installed\n", encoding="utf-8")
