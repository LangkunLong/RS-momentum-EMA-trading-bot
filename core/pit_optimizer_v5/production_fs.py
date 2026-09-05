"""Pinned local filesystem primitives for PIT optimizer V5 production adapters."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
from typing import BinaryIO

from core.pit_optimizer_artifacts import (
    _DirectoryAccess,
    _acquire_absolute_directory,
    _acquire_directory,
    _sync_directory_access,
    _windows_extended_path,
)


def _closed_name(name: str) -> str:
    if (
        type(name) is not str
        or not name
        or name in {".", ".."}
        or any(character in name for character in ("/", "\\", "\x00"))
    ):
        raise ValueError("filesystem child name is invalid")
    return name


def _is_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def acquire_absolute_directory_v5(
    path: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> _DirectoryAccess:
    """Pin every absolute path component without delete sharing."""

    access = _acquire_absolute_directory(path)
    if expected_identity is not None and access.identity != expected_identity:
        access.close()
        raise ValueError("directory identity changed")
    return access


def acquire_directory_v5(
    root: Path,
    parts: tuple[str, ...],
    *,
    create: bool,
    expected_root_identity: tuple[int, int],
) -> _DirectoryAccess:
    """Pin one root-relative directory chain, optionally creating components."""

    if type(parts) is not tuple:
        raise ValueError("directory components are invalid")
    for part in parts:
        _closed_name(part)
    return _acquire_directory(
        root,
        parts,
        create=create,
        expected_root_identity=expected_root_identity,
    )


def _open_windows_regular(
    path: Path,
    *,
    writable: bool,
    create_new: bool,
) -> int:
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    desired_access = 0x80000000 | (0x40000000 if writable else 0)  # GENERIC_READ | WRITE
    handle = create_file(
        _windows_extended_path(path),
        desired_access,
        0x0001,  # read sharing only; deny in-place writes and deletion while pinned
        None,
        1 if create_new else 3,  # CREATE_NEW / OPEN_EXISTING
        0x00000080 | 0x00200000,  # ATTRIBUTE_NORMAL | OPEN_REPARSE_POINT
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in {None, invalid}:
        error = ctypes.get_last_error()
        if create_new and error in {80, 183}:  # ERROR_FILE_EXISTS / ALREADY_EXISTS
            raise FileExistsError(str(path))
        raise ctypes.WinError(error)
    flags = (os.O_RDWR if writable else os.O_RDONLY) | getattr(os, "O_BINARY", 0)
    try:
        return msvcrt.open_osfhandle(int(handle), flags)
    except BaseException:
        kernel32.CloseHandle(wintypes.HANDLE(handle))
        raise


def _open_regular_descriptor(
    directory: _DirectoryAccess,
    name: str,
    *,
    writable: bool,
    create_new: bool,
) -> tuple[int, os.stat_result]:
    child = _closed_name(name)
    directory.assert_current()
    before: os.stat_result | None = None
    if not create_new:
        before = (
            os.lstat(directory.path / child)
            if os.name == "nt"
            else os.stat(child, dir_fd=directory.descriptor, follow_symlinks=False)
        )
        if _is_reparse(before) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("filesystem child is not an exact regular file")
    if os.name == "nt":
        descriptor = _open_windows_regular(
            directory.path / child,
            writable=writable,
            create_new=create_new,
        )
    else:
        flags = (os.O_RDWR if writable else os.O_RDONLY) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        if create_new:
            flags |= os.O_CREAT | os.O_EXCL
        descriptor = os.open(child, flags, 0o600, dir_fd=directory.descriptor)
    try:
        opened = os.fstat(descriptor)
        after = (
            os.lstat(directory.path / child)
            if os.name == "nt"
            else os.stat(child, dir_fd=directory.descriptor, follow_symlinks=False)
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _is_reparse(after)
            or not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or _identity(opened) != _identity(after)
            or (before is not None and _identity(before) != _identity(opened))
        ):
            raise ValueError("filesystem child identity changed")
        directory.assert_current()
        return descriptor, opened
    except BaseException:
        os.close(descriptor)
        raise


def open_regular_in_directory_v5(
    directory: _DirectoryAccess,
    name: str,
    *,
    writable: bool,
) -> tuple[BinaryIO, os.stat_result]:
    descriptor, info = _open_regular_descriptor(
        directory,
        name,
        writable=writable,
        create_new=False,
    )
    return os.fdopen(descriptor, "r+b" if writable else "rb", buffering=0), info


def read_regular_in_directory_v5(
    directory: _DirectoryAccess,
    name: str,
    *,
    maximum_bytes: int | None = None,
) -> tuple[bytes, os.stat_result]:
    if maximum_bytes is not None and (type(maximum_bytes) is not int or maximum_bytes <= 0):
        raise ValueError("filesystem read bound is invalid")
    stream, info = open_regular_in_directory_v5(directory, name, writable=False)
    try:
        content = stream.read(-1 if maximum_bytes is None else maximum_bytes + 1)
    finally:
        stream.close()
    if maximum_bytes is not None and len(content) > maximum_bytes:
        raise ValueError("filesystem child exceeds its read bound")
    directory.assert_current()
    return content, info


def hash_regular_in_directory_v5(
    directory: _DirectoryAccess,
    name: str,
    *,
    maximum_bytes: int | None = None,
) -> tuple[int, int, int, str]:
    content, info = read_regular_in_directory_v5(
        directory,
        name,
        maximum_bytes=maximum_bytes,
    )
    if info.st_size != len(content):
        raise ValueError("filesystem child changed during hashing")
    return info.st_dev, info.st_ino, info.st_size, hashlib.sha256(content).hexdigest()


def write_regular_in_directory_v5(
    directory: _DirectoryAccess,
    name: str,
    content: bytes,
) -> None:
    if type(content) is not bytes:
        raise ValueError("filesystem write content is invalid")
    stream, before = open_regular_in_directory_v5(directory, name, writable=True)
    try:
        stream.seek(0)
        stream.truncate(0)
        if stream.write(content) != len(content):
            raise OSError("filesystem write was incomplete")
        stream.flush()
        os.fsync(stream.fileno())
        after = os.fstat(stream.fileno())
        if _identity(before) != _identity(after):
            raise ValueError("filesystem child changed during write")
    finally:
        stream.close()
    directory.assert_current()


def write_new_regular_in_directory_v5(
    directory: _DirectoryAccess,
    name: str,
    content: bytes,
) -> None:
    if type(content) is not bytes:
        raise ValueError("filesystem create content is invalid")
    descriptor, _ = _open_regular_descriptor(
        directory,
        name,
        writable=True,
        create_new=True,
    )
    try:
        with os.fdopen(descriptor, "wb", buffering=0) as stream:
            if stream.write(content) != len(content):
                raise OSError("filesystem create was incomplete")
            stream.flush()
            os.fsync(stream.fileno())
        _sync_directory_access(directory)
        directory.assert_current()
    except BaseException:
        try:
            directory.unlink(name)
        except OSError:
            pass
        raise


__all__ = [
    "acquire_absolute_directory_v5",
    "acquire_directory_v5",
    "hash_regular_in_directory_v5",
    "open_regular_in_directory_v5",
    "read_regular_in_directory_v5",
    "write_new_regular_in_directory_v5",
    "write_regular_in_directory_v5",
]
