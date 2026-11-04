"""Windows process queries for the launcher (kernel32/psapi through ctypes; only imported on Windows): the processes
of a tree and their working sets, the Windows counterpart of `ps -o pgid=,rss=`."""

from __future__ import annotations

import ctypes
from ctypes import wintypes

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_kernel32.Process32FirstW.restype = wintypes.BOOL
_kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_kernel32.Process32NextW.restype = wintypes.BOOL
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD]
_kernel32.K32GetProcessMemoryInfo.restype = wintypes.BOOL


def parents() -> dict[int, int]:
    """pid -> parent pid of every running process."""
    snapshot = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot in (None, INVALID_HANDLE_VALUE):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        result: dict[int, int] = {}
        ok = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            result[entry.th32ProcessID] = entry.th32ParentProcessID
            ok = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return result
    finally:
        _kernel32.CloseHandle(snapshot)


def tree(root: int, parent_of: dict[int, int]) -> list[int]:
    """`root` and its descendants (a parent pid that was reused by a newer process is no parent: children are only
    followed from processes found in the tree, and each pid once)."""
    children: dict[int, list[int]] = {}
    for pid, parent in parent_of.items():
        if pid != parent:
            children.setdefault(parent, []).append(pid)
    found, todo = [], [root] if root in parent_of else []
    seen = set(todo)
    while todo:
        pid = todo.pop()
        found.append(pid)
        for child in children.get(pid, ()):
            if child not in seen:
                seen.add(child)
                todo.append(child)
    return found


def working_set_kib(pid: int) -> int:
    """The working set (resident memory) of a process in KiB, 0 if it cannot be read (exited, no access)."""
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        return 0
    try:
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        if not _kernel32.K32GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return 0
        return counters.WorkingSetSize // 1024
    finally:
        _kernel32.CloseHandle(handle)


def tree_working_set_kib(roots: list[int]) -> dict[int, int]:
    """Working set in KiB of each root's process tree."""
    parent_of = parents()
    return {root: sum(working_set_kib(pid) for pid in tree(root, parent_of)) for root in roots}
