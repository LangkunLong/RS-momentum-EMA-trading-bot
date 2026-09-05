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
    if maximum_bytes is not None and (type(maximum_bytes) is not int or maximum_bytes <= 0):
        raise ValueError("filesystem hash bound is invalid")
    stream, info = open_regular_in_directory_v5(directory, name, writable=False)
    if maximum_bytes is not None and info.st_size > maximum_bytes:
        stream.close()
        raise ValueError("filesystem child exceeds its hash bound")
    digest = hashlib.sha256()
    observed_bytes = 0
    try:
        while chunk := stream.read(1024 * 1024):
            observed_bytes += len(chunk)
            if maximum_bytes is not None and observed_bytes > maximum_bytes:
                raise ValueError("filesystem child exceeds its hash bound")
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    finally:
        stream.close()
    if (
        observed_bytes != info.st_size
        or _identity(after) != _identity(info)
        or after.st_size != info.st_size
    ):
        raise ValueError("filesystem child changed during hashing")
    directory.assert_current()
    return info.st_dev, info.st_ino, observed_bytes, digest.hexdigest()


def _windows_handle_identity(handle: int) -> int:
    import ctypes
    from ctypes import wintypes

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = kernel32.GetFileInformationByHandle
    get_info.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation))
    get_info.restype = wintypes.BOOL
    value = _ByHandleFileInformation()
    if not get_info(wintypes.HANDLE(handle), ctypes.byref(value)):
        raise ctypes.WinError(ctypes.get_last_error())
    return (int(value.nFileIndexHigh) << 32) | int(value.nFileIndexLow)


def _open_windows_delete_handle(path: Path, *, directory: bool) -> tuple[int, os.stat_result]:
    import ctypes
    from ctypes import wintypes

    before = os.lstat(path)
    if _is_reparse(before) or stat.S_ISDIR(before.st_mode) is not directory:
        raise ValueError("filesystem cleanup target is not an exact expected entry")
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
    handle = create_file(
        _windows_extended_path(path),
        0x00010000 | 0x00000080 | (0x00000001 if directory else 0),
        0x0001 | 0x0002 | 0x0004,
        None,
        3,
        0x00200000 | (0x02000000 if directory else 0x00000080),
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in {None, invalid}:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        after = os.lstat(path)
        handle_inode = _windows_handle_identity(int(handle))
        if (
            _is_reparse(after)
            or stat.S_ISDIR(after.st_mode) is not directory
            or _identity(before) != _identity(after)
            or handle_inode != after.st_ino
        ):
            raise ValueError("filesystem cleanup target changed while opening")
        return int(handle), after
    except BaseException:
        kernel32.CloseHandle(wintypes.HANDLE(handle))
        raise


def _mark_windows_handle_for_delete(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    class _FileDispositionInfo(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOL)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_info = kernel32.SetFileInformationByHandle
    set_info.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    set_info.restype = wintypes.BOOL
    disposition = _FileDispositionInfo(True)
    if not set_info(
        wintypes.HANDLE(handle),
        4,  # FileDispositionInfo
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _close_windows_raw_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel32.CloseHandle(wintypes.HANDLE(handle)):
        raise ctypes.WinError(ctypes.get_last_error())


def _remove_windows_tree(path: Path, *, expected_identity: tuple[int, int]) -> None:
    handle, info = _open_windows_delete_handle(path, directory=True)
    try:
        if _identity(info) != expected_identity:
            raise ValueError("filesystem cleanup root identity changed")
        for entry in tuple(os.scandir(path)):
            child = path / _closed_name(entry.name)
            child_info = os.lstat(child)
            if _is_reparse(child_info):
                raise ValueError("filesystem cleanup encountered a reparse point")
            if stat.S_ISDIR(child_info.st_mode):
                _remove_windows_tree(child, expected_identity=_identity(child_info))
            elif stat.S_ISREG(child_info.st_mode) and child_info.st_nlink == 1:
                child_handle, opened = _open_windows_delete_handle(child, directory=False)
                try:
                    if _identity(opened) != _identity(child_info):
                        raise ValueError("filesystem cleanup child identity changed")
                    _mark_windows_handle_for_delete(child_handle)
                finally:
                    _close_windows_raw_handle(child_handle)
            else:
                raise ValueError("filesystem cleanup encountered a foreign entry")
        current = os.lstat(path)
        if _identity(current) != expected_identity or _windows_handle_identity(handle) != current.st_ino:
            raise ValueError("filesystem cleanup root identity changed")
        _mark_windows_handle_for_delete(handle)
    finally:
        _close_windows_raw_handle(handle)


def _remove_posix_tree(
    parent_descriptor: int,
    name: str,
    *,
    expected_identity: tuple[int, int],
) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode) or _identity(info) != expected_identity:
            raise ValueError("filesystem cleanup root identity changed")
        for child in tuple(os.listdir(descriptor)):
            child = _closed_name(child)
            child_info = os.stat(child, dir_fd=descriptor, follow_symlinks=False)
            if _is_reparse(child_info):
                raise ValueError("filesystem cleanup encountered a link")
            if stat.S_ISDIR(child_info.st_mode):
                _remove_posix_tree(descriptor, child, expected_identity=_identity(child_info))
            elif stat.S_ISREG(child_info.st_mode) and child_info.st_nlink == 1:
                child_descriptor = os.open(
                    child,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
                try:
                    opened = os.fstat(child_descriptor)
                    current = os.stat(child, dir_fd=descriptor, follow_symlinks=False)
                    if _identity(opened) != _identity(current) or _identity(current) != _identity(child_info):
                        raise ValueError("filesystem cleanup child identity changed")
                    os.unlink(child, dir_fd=descriptor)
                finally:
                    os.close(child_descriptor)
            else:
                raise ValueError("filesystem cleanup encountered a foreign entry")
        if _identity(os.fstat(descriptor)) != expected_identity:
            raise ValueError("filesystem cleanup root identity changed")
    finally:
        os.close(descriptor)
    os.rmdir(name, dir_fd=parent_descriptor)


def remove_owned_tree_in_directory_v5(
    parent: _DirectoryAccess,
    name: str,
    *,
    expected_identity: tuple[int, int],
) -> None:
    """Delete one exact owned child without following or crossing filesystem links."""

    child = _closed_name(name)
    if (
        type(expected_identity) is not tuple
        or len(expected_identity) != 2
        or any(type(item) is not int for item in expected_identity)
    ):
        raise ValueError("filesystem cleanup identity is invalid")
    parent.assert_current()
    if os.name == "nt":
        _remove_windows_tree(parent.path / child, expected_identity=expected_identity)
    else:
        _remove_posix_tree(parent.descriptor, child, expected_identity=expected_identity)
    parent.assert_current()
    try:
        if os.name == "nt":
            os.lstat(parent.path / child)
        else:
            os.stat(child, dir_fd=parent.descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise ValueError("filesystem cleanup target remains")


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
    "remove_owned_tree_in_directory_v5",
    "write_new_regular_in_directory_v5",
    "write_regular_in_directory_v5",
]
