"""Pinned local filesystem primitives for PIT optimizer V5 production adapters."""

from __future__ import annotations

import hashlib
import ntpath
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
            os.lstat(_windows_extended_path(directory.path / child))
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
            os.lstat(_windows_extended_path(directory.path / child))
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


def _windows_handle_information(handle: int) -> tuple[int, int, int, int]:
    """Return attributes, volume serial, file id, and link count for one handle."""

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
    return (
        int(value.dwFileAttributes),
        int(value.dwVolumeSerialNumber),
        (int(value.nFileIndexHigh) << 32) | int(value.nFileIndexLow),
        int(value.nNumberOfLinks),
    )


def _windows_handle_identity(handle: int) -> int:
    return _windows_handle_information(handle)[2]


def _windows_final_path(handle: int) -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_name = kernel32.GetFinalPathNameByHandleW
    get_name.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    get_name.restype = wintypes.DWORD
    required = int(get_name(wintypes.HANDLE(handle), None, 0, 0))
    if required <= 0 or required > 32768:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_unicode_buffer(required + 1)
    written = int(get_name(wintypes.HANDLE(handle), buffer, len(buffer), 0))
    if written <= 0 or written >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    return buffer.value


def _windows_directory_handle(directory: _DirectoryAccess) -> int:
    """Return the held leaf handle without accepting an unpinned path."""

    directory.assert_current()
    handles = getattr(directory, "_windows_handles", None)
    if os.name != "nt" or type(handles) is not list or not handles:
        raise RuntimeError("component-relative Windows directory handle is unavailable")
    handle = handles[-1]
    if type(handle) is not int or handle <= 0:
        raise RuntimeError("component-relative Windows directory handle is invalid")
    return handle


def _open_windows_enumeration_handle(directory: _DirectoryAccess) -> int:
    """Open list authority for the exact directory already pinned by ``directory``."""

    import ctypes
    from ctypes import wintypes

    directory.assert_current()
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
        _windows_extended_path(directory.path),
        0x00000001 | 0x00000080,  # FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES
        0x0001 | 0x0002,  # READ | WRITE sharing; held chain denies delete/rename
        None,
        3,
        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in {None, invalid}:
        raise ctypes.WinError(ctypes.get_last_error())
    raw = int(handle)
    try:
        attributes, _volume_serial, file_id, _links = _windows_handle_information(raw)
        if (
            attributes & 0x00000400
            or not attributes & 0x00000010
            or file_id != directory.identity[1]
        ):
            raise ValueError("filesystem directory identity changed before enumeration")
        directory.assert_current()
        return raw
    except BaseException:
        _close_windows_raw_handle(raw)
        raise


def _raise_windows_ntstatus(status: int, name: str) -> None:
    import ctypes

    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    convert = ntdll.RtlNtStatusToDosError
    convert.argtypes = (ctypes.c_ulong,)
    convert.restype = ctypes.c_ulong
    error = int(convert(ctypes.c_ulong(status & 0xFFFFFFFF)))
    if error in {2, 3}:  # ERROR_FILE_NOT_FOUND / ERROR_PATH_NOT_FOUND
        raise FileNotFoundError(name)
    if error in {80, 183}:  # ERROR_FILE_EXISTS / ERROR_ALREADY_EXISTS
        raise FileExistsError(name)
    if error in {5, 32}:  # ACCESS_DENIED / SHARING_VIOLATION
        raise PermissionError(error, "Windows relative entry could not be pinned", name)
    if error == 267:  # ERROR_DIRECTORY
        raise NotADirectoryError(name)
    raise OSError(error, "Windows relative entry open failed", name)


def _create_windows_directory_at(parent: _DirectoryAccess, name: str) -> int:
    """Atomically create and pin one directory relative to ``parent``."""

    import ctypes
    from ctypes import wintypes

    child = _closed_name(name)

    class _UnicodeString(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", ctypes.POINTER(ctypes.c_ushort)),
        ]

    class _ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(_UnicodeString)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", wintypes.LPVOID),
            ("SecurityQualityOfService", wintypes.LPVOID),
        ]

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

    encoded = child.encode("utf-16-le")
    name_buffer = (ctypes.c_ushort * (len(encoded) // 2 + 1))()
    ctypes.memmove(name_buffer, encoded, len(encoded))
    unicode_name = _UnicodeString(
        len(encoded),
        len(encoded),
        ctypes.cast(name_buffer, ctypes.POINTER(ctypes.c_ushort)),
    )
    attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        wintypes.HANDLE(_windows_directory_handle(parent)),
        ctypes.pointer(unicode_name),
        0x00000040,  # OBJ_CASE_INSENSITIVE
        None,
        None,
    )
    io_status = _IoStatusBlock()
    handle = wintypes.HANDLE()
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    nt_create = ntdll.NtCreateFile
    nt_create.argtypes = (
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.ULONG,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.LPVOID,
        wintypes.ULONG,
    )
    nt_create.restype = ctypes.c_long
    status = int(
        nt_create(
            ctypes.byref(handle),
            0x00000001 | 0x00000080 | 0x00010000 | 0x00100000,
            ctypes.byref(attributes),
            ctypes.byref(io_status),
            None,
            0x00000010,  # FILE_ATTRIBUTE_DIRECTORY
            0x00000001 | 0x00000002,  # read/write sharing; deny delete/rename
            2,  # FILE_CREATE: fail atomically if the child exists
            0x00000001 | 0x00000020 | 0x00200000,
            None,
            0,
        )
    )
    if status < 0:
        _raise_windows_ntstatus(status, child)
    raw = int(handle.value)
    try:
        file_attributes, _volume_serial, _file_id, _links = _windows_handle_information(raw)
        if file_attributes & 0x00000400 or not file_attributes & 0x00000010:
            raise ValueError("created directory is a reparse point or non-directory")
        parent.assert_current()
        return raw
    except BaseException:
        _close_windows_raw_handle(raw)
        raise


def _open_windows_delete_handle_at(
    parent_handle: int,
    name: str,
    *,
    directory: bool,
) -> tuple[int, tuple[int, int, int, int]]:
    """Open one child relative to an already pinned directory handle."""

    import ctypes
    from ctypes import wintypes

    child = _closed_name(name)

    class _UnicodeString(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", ctypes.POINTER(ctypes.c_ushort)),
        ]

    class _ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(_UnicodeString)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", wintypes.LPVOID),
            ("SecurityQualityOfService", wintypes.LPVOID),
        ]

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

    encoded = child.encode("utf-16-le")
    name_buffer = (ctypes.c_ushort * (len(encoded) // 2 + 1))()
    ctypes.memmove(name_buffer, encoded, len(encoded))
    unicode_name = _UnicodeString(
        len(encoded),
        len(encoded),
        ctypes.cast(name_buffer, ctypes.POINTER(ctypes.c_ushort)),
    )
    attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        wintypes.HANDLE(parent_handle),
        ctypes.pointer(unicode_name),
        0x00000040,  # OBJ_CASE_INSENSITIVE
        None,
        None,
    )
    io_status = _IoStatusBlock()
    handle = wintypes.HANDLE()
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    nt_create = ntdll.NtCreateFile
    nt_create.argtypes = (
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.ULONG,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.LPVOID,
        wintypes.ULONG,
    )
    nt_create.restype = ctypes.c_long
    options = 0x00200000 | 0x00000020  # OPEN_REPARSE_POINT | SYNCHRONOUS_IO_NONALERT
    options |= 0x00000001 if directory else 0x00000040  # DIRECTORY / NON_DIRECTORY
    status = int(
        nt_create(
            ctypes.byref(handle),
            0x00010000 | 0x00100000 | 0x00000080 | 0x00000001,
            ctypes.byref(attributes),
            ctypes.byref(io_status),
            None,
            0,
            0x00000001,  # FILE_SHARE_READ: deny mutation/rename while pinned
            1,  # FILE_OPEN
            options,
            None,
            0,
        )
    )
    if status < 0:
        _raise_windows_ntstatus(status, child)
    raw_handle = int(handle.value)
    try:
        information = _windows_handle_information(raw_handle)
        file_attributes, _volume_serial, _file_id, link_count = information
        if (
            bool(file_attributes & 0x00000400)  # FILE_ATTRIBUTE_REPARSE_POINT
            or bool(file_attributes & 0x00000010) is not directory
            or (not directory and link_count != 1)
        ):
            raise ValueError("filesystem cleanup target is not an exact expected entry")
        return raw_handle, information
    except BaseException:
        _close_windows_raw_handle(raw_handle)
        raise


def _try_open_windows_delete_handle_at(
    parent_handle: int,
    name: str,
    *,
    directory: bool,
) -> tuple[int, tuple[int, int, int, int]] | None:
    try:
        return _open_windows_delete_handle_at(parent_handle, name, directory=directory)
    except FileNotFoundError:
        return None


def _rename_windows_handle_at(handle: int, parent: _DirectoryAccess, name: str) -> None:
    """Atomically move an exact open handle beneath its pinned parent."""

    import ctypes
    from ctypes import wintypes

    class _FileRenameInfo(ctypes.Structure):
        _fields_ = [
            ("ReplaceIfExists", ctypes.c_ubyte),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", ctypes.c_ushort * 1),
        ]

    target = parent.path / _closed_name(name)
    encoded = str(target).encode("utf-16-le")
    # Include the structure's trailing WCHAR storage and a zeroed terminator;
    # FileNameLength remains the exact non-NUL byte count.
    size = ctypes.sizeof(_FileRenameInfo) + len(encoded)
    buffer = ctypes.create_string_buffer(size)
    value = ctypes.cast(buffer, ctypes.POINTER(_FileRenameInfo)).contents
    value.ReplaceIfExists = 0
    # SetFileInformationByHandle requires an absolute name and a null root.
    # ``parent`` remains pinned without delete sharing for this whole call, so
    # the absolute namespace cannot be exchanged while the exact target handle
    # is quarantined.
    value.RootDirectory = None
    value.FileNameLength = len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + _FileRenameInfo.FileName.offset, encoded, len(encoded))
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_info = kernel32.SetFileInformationByHandle
    set_info.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    set_info.restype = wintypes.BOOL
    if not set_info(wintypes.HANDLE(handle), 3, buffer, size):  # FileRenameInfo
        raise ctypes.WinError(ctypes.get_last_error())


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


def _enumerate_windows_directory(handle: int) -> tuple[tuple[str, int, int], ...]:
    """Enumerate names, attributes, and file ids from the held directory handle."""

    import ctypes
    from ctypes import wintypes

    class _FileIdBothDirectoryInfo(ctypes.Structure):
        _fields_ = [
            ("NextEntryOffset", wintypes.DWORD),
            ("FileIndex", wintypes.DWORD),
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("EndOfFile", ctypes.c_longlong),
            ("AllocationSize", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
            ("FileNameLength", wintypes.DWORD),
            ("EaSize", wintypes.DWORD),
            ("ShortNameLength", ctypes.c_ubyte),
            ("ShortName", ctypes.c_ushort * 12),
            ("FileId", ctypes.c_longlong),
            ("FileName", ctypes.c_ushort * 1),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    query = kernel32.GetFileInformationByHandleEx
    query.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    query.restype = wintypes.BOOL
    buffer_size = 1024 * 1024
    buffer = ctypes.create_string_buffer(buffer_size)
    result: list[tuple[str, int, int]] = []
    seen: set[str] = set()
    info_class = 11  # FileIdBothDirectoryRestartInfo for the first page
    while True:
        ctypes.memset(buffer, 0, buffer_size)
        if not query(wintypes.HANDLE(handle), info_class, buffer, buffer_size):
            error = ctypes.get_last_error()
            if error == 18:  # ERROR_NO_MORE_FILES
                break
            raise ctypes.WinError(error)
        info_class = 10  # FileIdBothDirectoryInfo continues enumeration
        offset = 0
        while True:
            if offset + _FileIdBothDirectoryInfo.FileName.offset > buffer_size:
                raise ValueError("filesystem cleanup directory enumeration is malformed")
            entry = _FileIdBothDirectoryInfo.from_buffer_copy(buffer, offset)
            length = int(entry.FileNameLength)
            name_offset = offset + _FileIdBothDirectoryInfo.FileName.offset
            if length % 2 or length <= 0 or name_offset + length > buffer_size:
                raise ValueError("filesystem cleanup directory name is malformed")
            raw_name = ctypes.string_at(ctypes.addressof(buffer) + name_offset, length)
            try:
                name = raw_name.decode("utf-16-le", errors="strict")
            except UnicodeDecodeError:
                raise ValueError("filesystem cleanup directory name is malformed") from None
            if name not in {".", ".."}:
                child = _closed_name(name)
                key = child.casefold()
                if key in seen or len(result) >= 1_000_000:
                    raise ValueError("filesystem cleanup directory enumeration is ambiguous")
                seen.add(key)
                result.append((child, int(entry.FileAttributes), int(entry.FileId) & ((1 << 64) - 1)))
            next_offset = int(entry.NextEntryOffset)
            if next_offset == 0:
                break
            if next_offset < _FileIdBothDirectoryInfo.FileName.offset or offset + next_offset <= offset:
                raise ValueError("filesystem cleanup directory enumeration is malformed")
            offset += next_offset
    return tuple(result)


def _clear_open_windows_directory(
    handle: int,
    *,
    expected_identity: tuple[int, int],
) -> None:
    """Delete only descendants opened and pinned beneath ``handle``."""

    attributes, _volume_serial, file_id, _link_count = _windows_handle_information(handle)
    if attributes & 0x00000400 or not attributes & 0x00000010 or file_id != expected_identity[1]:
        raise ValueError("filesystem cleanup root identity changed")
    for child, enumerated_attributes, enumerated_file_id in _enumerate_windows_directory(handle):
        if enumerated_attributes & 0x00000400:
            raise ValueError("filesystem cleanup encountered a reparse point")
        is_directory = bool(enumerated_attributes & 0x00000010)
        child_handle, opened = _open_windows_delete_handle_at(
            handle,
            child,
            directory=is_directory,
        )
        try:
            opened_attributes, _opened_volume, opened_file_id, _opened_links = opened
            if (
                opened_file_id != enumerated_file_id
                or bool(opened_attributes & 0x00000010) is not is_directory
                or bool(opened_attributes & 0x00000400)
            ):
                raise ValueError("filesystem cleanup child identity changed")
            if is_directory:
                _remove_open_windows_tree(
                    child_handle,
                    expected_identity=(expected_identity[0], opened_file_id),
                )
            else:
                _mark_windows_handle_for_delete(child_handle)
        finally:
            _close_windows_raw_handle(child_handle)
    if _enumerate_windows_directory(handle):
        raise ValueError("filesystem cleanup directory changed during deletion")
    if _windows_handle_identity(handle) != expected_identity[1]:
        raise ValueError("filesystem cleanup root identity changed")


def _remove_open_windows_tree(
    handle: int,
    *,
    expected_identity: tuple[int, int],
) -> None:
    """Delete one exact open directory after clearing its descendants."""

    _clear_open_windows_directory(handle, expected_identity=expected_identity)
    _mark_windows_handle_for_delete(handle)


def _remove_windows_tree(
    parent: _DirectoryAccess,
    name: str,
    *,
    expected_identity: tuple[int, int],
) -> bool:
    """Quarantine an exact owned root by handle, then recursively disposition it."""

    quarantine_name = ".pit-v5-quarantine-" + hashlib.sha256(
        f"{name}:{expected_identity[0]}:{expected_identity[1]}".encode("utf-8")
    ).hexdigest()[:32]
    parent_handle = _windows_directory_handle(parent)
    original = _try_open_windows_delete_handle_at(parent_handle, name, directory=True)
    quarantine = _try_open_windows_delete_handle_at(parent_handle, quarantine_name, directory=True)
    if original is not None and quarantine is not None:
        _close_windows_raw_handle(original[0])
        _close_windows_raw_handle(quarantine[0])
        raise ValueError("filesystem cleanup has conflicting namespace entries")
    selected = original if original is not None else quarantine
    if selected is None:
        return False
    handle, opened = selected
    try:
        if opened[2] != expected_identity[1]:
            raise ValueError("filesystem cleanup root identity changed")
        if original is not None:
            _rename_windows_handle_at(handle, parent, quarantine_name)
            expected_final = _windows_extended_path(parent.path / quarantine_name)
            if ntpath.normcase(ntpath.normpath(_windows_final_path(handle))) != ntpath.normcase(
                ntpath.normpath(expected_final)
            ):
                raise ValueError("filesystem cleanup quarantine identity changed")
            unexpected_original = _try_open_windows_delete_handle_at(
                parent_handle,
                name,
                directory=True,
            )
            if unexpected_original is not None:
                _close_windows_raw_handle(unexpected_original[0])
                raise ValueError("filesystem cleanup quarantine identity changed")
        _remove_open_windows_tree(handle, expected_identity=expected_identity)
    finally:
        _close_windows_raw_handle(handle)
    for candidate in (name, quarantine_name):
        remaining = _try_open_windows_delete_handle_at(
            parent_handle,
            candidate,
            directory=True,
        )
        if remaining is not None:
            _close_windows_raw_handle(remaining[0])
            raise ValueError("filesystem cleanup target remains")
    return True


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
) -> bool:
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
        removed = _remove_windows_tree(parent, child, expected_identity=expected_identity)
        parent.assert_current()
        return removed
    try:
        _remove_posix_tree(parent.descriptor, child, expected_identity=expected_identity)
    except FileNotFoundError:
        parent.assert_current()
        return False
    parent.assert_current()
    try:
        os.stat(child, dir_fd=parent.descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return True
    raise ValueError("filesystem cleanup target remains")


def create_directory_in_directory_v5(
    parent: _DirectoryAccess,
    name: str,
) -> _DirectoryAccess:
    """Atomically create one owned child and return a held exact capability.

    The production adapters are intentionally Windows-only: POSIX cannot both
    create a directory and receive its descriptor in one namespace operation.
    """

    child = _closed_name(name)
    parent.assert_current()
    if os.name != "nt":
        raise RuntimeError("atomic directory creation requires the Windows production adapter")
    handle = _create_windows_directory_at(parent, child)
    information = _windows_handle_information(handle)
    identity = (parent.identity[0], information[2])
    try:
        access = acquire_directory_v5(
            parent.path,
            (child,),
            create=False,
            expected_root_identity=parent.identity,
        )
        if access.identity[1] != information[2]:
            access.close()
            raise ValueError("created directory identity changed while binding authority")
        parent.assert_current()
    except BaseException:
        try:
            _remove_open_windows_tree(handle, expected_identity=identity)
        finally:
            _close_windows_raw_handle(handle)
        raise
    # ``access`` independently pins the same child before creation authority is
    # released, so no namespace gap exists between create and adoption.
    _close_windows_raw_handle(handle)
    return access


def clear_owned_directory_v5(directory: _DirectoryAccess) -> None:
    """Clear one exact owned directory without reopening or removing its root."""

    directory.assert_current()
    if os.name != "nt":
        raise RuntimeError("owned directory clearing requires the Windows production adapter")
    handle = _open_windows_enumeration_handle(directory)
    try:
        _clear_open_windows_directory(handle, expected_identity=directory.identity)
    finally:
        _close_windows_raw_handle(handle)
    directory.assert_current()


def directory_entry_names_v5(directory: _DirectoryAccess) -> tuple[str, ...]:
    """List closed names from a held directory, never from a reopened path."""

    directory.assert_current()
    if os.name == "nt":
        handle = _open_windows_enumeration_handle(directory)
        try:
            names = tuple(item[0] for item in _enumerate_windows_directory(handle))
        finally:
            _close_windows_raw_handle(handle)
    else:
        names = tuple(_closed_name(item) for item in os.listdir(directory.descriptor))
    directory.assert_current()
    return tuple(sorted(names, key=str.casefold))


def directory_is_empty_v5(directory: _DirectoryAccess) -> bool:
    """Inspect a held directory by descriptor/handle, never by a reopened path."""

    return not directory_entry_names_v5(directory)


def directory_child_absent_v5(directory: _DirectoryAccess, name: str) -> bool:
    """Prove a closed child name absent relative to one pinned directory."""

    child = _closed_name(name)
    directory.assert_current()
    if os.name == "nt":
        opened = _try_open_windows_delete_handle_at(
            _windows_directory_handle(directory),
            child,
            directory=True,
        )
        if opened is None:
            directory.assert_current()
            return True
        _close_windows_raw_handle(opened[0])
        directory.assert_current()
        return False
    try:
        metadata = os.stat(child, dir_fd=directory.descriptor, follow_symlinks=False)
    except FileNotFoundError:
        directory.assert_current()
        return True
    if _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("filesystem child is not an exact directory")
    directory.assert_current()
    return False


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
    "clear_owned_directory_v5",
    "create_directory_in_directory_v5",
    "directory_child_absent_v5",
    "directory_entry_names_v5",
    "directory_is_empty_v5",
    "hash_regular_in_directory_v5",
    "open_regular_in_directory_v5",
    "read_regular_in_directory_v5",
    "remove_owned_tree_in_directory_v5",
    "write_new_regular_in_directory_v5",
    "write_regular_in_directory_v5",
]
