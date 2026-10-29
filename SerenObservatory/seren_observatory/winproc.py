"""
What a node reads off /proc on Linux, read off Win32 on Windows.

The Observatory on a Windows box (a desktop running Symposium and a
few NSSM services) used to answer None for everything /proc gives a Linux
node - a service's memory, CPU and uptime, the box's memory and uptime - and
its port probes waited the full timeout on a down service, because a
Windows box with its firewall up DROPS a SYN to a closed port instead of
refusing it, even on loopback (measured 28 Oct 2026: every closed local
port took the whole 2s, three URLs per status, so a down service cost a
poll six seconds and the desktop read as unreachable from Lodestar).

Everything here is ctypes over kernel32 / psapi / iphlpapi, so the base
install stays pyyaml-only. Every function answers None (or an empty
collection) off Windows or when Windows will not say, and never raises
into a status: a poll must not fall over a process that just ended.
"""
from __future__ import annotations

import asyncio
import os
import socket
import time
from typing import Any, Optional

IS_WINDOWS = os.name == "nt"

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x2
TCP_TABLE_OWNER_PID_LISTENER = 3
AF_INET, AF_INET6 = 2, 23
MIB_TCP_STATE_LISTEN = 2
#: FILETIME counts 100ns ticks since 1601-01-01; the epoch is this far before 1970.
_FILETIME_EPOCH_DIFF = 116444736000000000
#: The addresses a probe of "this box" may use; a listener on any local
#: address answers them.
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "0.0.0.0", "::1", "::"})


def _k32():
    import ctypes
    return ctypes.WinDLL("kernel32", use_last_error=True)


# ── the TCP table: who listens on a port ────────────────────────────────────

def listening_pids(port: int) -> Optional[set[int]]:
    """The pids with a TCP listener on `port` (v4 or v6, any local address).
    None off Windows or when the table could not be read; an empty set means
    nothing listens. Read-only: no socket is opened, no port is touched."""
    if not IS_WINDOWS or port <= 0:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        iphlp = ctypes.WinDLL("iphlpapi", use_last_error=True)
    except OSError:
        return None
    want = socket.htons(port)
    pids: set[int] = set()
    for family, row_type in ((AF_INET, _Row4), (AF_INET6, _Row6)):
        size = wintypes.DWORD(0)
        iphlp.GetExtendedTcpTable(None, ctypes.byref(size), False, family, TCP_TABLE_OWNER_PID_LISTENER, 0)
        if size.value == 0:
            continue
        buf = ctypes.create_string_buffer(size.value)
        rc = iphlp.GetExtendedTcpTable(buf, ctypes.byref(size), False, family, TCP_TABLE_OWNER_PID_LISTENER, 0)
        if rc != 0:
            if family == AF_INET:
                return None
            continue
        count = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0]
        rows = ctypes.cast(ctypes.addressof(buf) + ctypes.sizeof(wintypes.DWORD),
                           ctypes.POINTER(row_type * count))[0]
        for row in rows:
            if (row.dwLocalPort & 0xFFFF) == want and row.dwState == MIB_TCP_STATE_LISTEN:
                pids.add(int(row.dwOwningPid))
    return pids


def _define_rows():
    import ctypes
    from ctypes import wintypes

    class Row4(ctypes.Structure):          # MIB_TCPROW_OWNER_PID
        _fields_ = [("dwState", wintypes.DWORD), ("dwLocalAddr", wintypes.DWORD), ("dwLocalPort", wintypes.DWORD),
                    ("dwRemoteAddr", wintypes.DWORD), ("dwRemotePort", wintypes.DWORD), ("dwOwningPid", wintypes.DWORD)]

    class Row6(ctypes.Structure):          # MIB_TCP6ROW_OWNER_PID
        _fields_ = [("ucLocalAddr", ctypes.c_ubyte * 16), ("dwLocalScopeId", wintypes.DWORD),
                    ("dwLocalPort", wintypes.DWORD), ("ucRemoteAddr", ctypes.c_ubyte * 16),
                    ("dwRemoteScopeId", wintypes.DWORD), ("dwRemotePort", wintypes.DWORD),
                    ("dwState", wintypes.DWORD), ("dwOwningPid", wintypes.DWORD)]
    return Row4, Row6


if IS_WINDOWS:
    _Row4, _Row6 = _define_rows()


def port_is_listening(port: int) -> Optional[bool]:
    """True / False when Windows can say, None when it cannot (or off Windows)."""
    pids = listening_pids(port)
    return None if pids is None else bool(pids)


def host_is_this_box(host: str) -> bool:
    """A probe URL's host is this box when it is a loopback or wildcard
    name, or the box's own hostname."""
    h = (host or "").strip("[]").lower()
    if h in LOCAL_HOSTS:
        return True
    try:
        return h == socket.gethostname().lower()
    except OSError:
        return False


# ── processes ──────────────────────────────────────────────────────────────

def process_tree(pid: int) -> list[int]:
    """`pid` and every descendant, from one Toolhelp snapshot. An NSSM
    service is nssm.exe, then the venv's python.exe launcher, then the
    interpreter: three processes, and the memory that matters is the sum."""
    if not IS_WINDOWS or not pid:
        return [pid] if pid else []
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):      # PROCESSENTRY32W
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k32 = _k32()
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return [pid]
    children: dict[int, list[int]] = {}
    try:
        e = Entry()
        e.dwSize = ctypes.sizeof(Entry)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            children.setdefault(int(e.th32ParentProcessID), []).append(int(e.th32ProcessID))
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    out, todo, seen = [], [pid], set()
    while todo:
        p = todo.pop()
        if p in seen:
            continue                      # pids are reused; a stale parent link could loop
        seen.add(p)
        out.append(p)
        todo.extend(children.get(p, []))
    return out


def _open(pid: int):
    k32 = _k32()
    import ctypes
    from ctypes import wintypes
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    return k32, k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)


def _times(k32, handle) -> Optional[tuple[int, int]]:
    """(creation ticks, kernel+user ticks) in 100ns, or None."""
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    def val(ft):
        return (ft.dwHighDateTime << 32) | ft.dwLowDateTime
    c, x, k, u = FILETIME(), FILETIME(), FILETIME(), FILETIME()
    if not k32.GetProcessTimes(handle, ctypes.byref(c), ctypes.byref(x), ctypes.byref(k), ctypes.byref(u)):
        return None
    return val(c), val(k) + val(u)


def _working_set_bytes(handle) -> Optional[int]:
    import ctypes
    from ctypes import wintypes
    try:
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
    except OSError:
        return None

    class PMC(ctypes.Structure):         # PROCESS_MEMORY_COUNTERS
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
        return None
    return int(pmc.WorkingSetSize)


def process_stats(pid: int, tree: bool = True) -> dict[str, Any]:
    """{memory_mb, uptime_seconds, cpu_ticks, pids} for a process (and, with
    tree, its descendants: memory and CPU summed, uptime the root's). Empty
    values when Windows will not say."""
    out: dict[str, Any] = {"memory_mb": None, "uptime_seconds": None, "cpu_ticks": None, "pids": []}
    if not IS_WINDOWS or not pid:
        return out
    pids = process_tree(pid) if tree else [pid]
    mem, ticks, root_created = 0, 0, None
    seen_mem = seen_ticks = False
    for p in pids:
        try:
            k32, h = _open(p)
        except OSError:
            continue
        if not h:
            continue
        try:
            ws = _working_set_bytes(h)
            if ws is not None:
                mem += ws
                seen_mem = True
            t = _times(k32, h)
            if t is not None:
                ticks += t[1]
                seen_ticks = True
                if p == pid:
                    root_created = t[0]
        finally:
            k32.CloseHandle(h)
    out["pids"] = pids
    if seen_mem:
        out["memory_mb"] = mem // (1024 * 1024)
    if seen_ticks:
        out["cpu_ticks"] = ticks
    if root_created is not None:
        started = (root_created - _FILETIME_EPOCH_DIFF) / 10_000_000
        out["uptime_seconds"] = max(0, int(time.time() - started))
    return out


async def cpu_percent(pid: int, sample_ms: int = 100) -> Optional[float]:
    """CPU as percent of one core over `sample_ms`, for the process tree.
    100 = one core saturated, the same scale as the Linux reading."""
    if not IS_WINDOWS or not pid:
        return None
    a = process_stats(pid)
    if a["cpu_ticks"] is None:
        return None
    t0 = time.perf_counter()
    await asyncio.sleep(sample_ms / 1000.0)
    b = process_stats(pid)
    wall = time.perf_counter() - t0
    if b["cpu_ticks"] is None or wall <= 0:
        return None
    used = (b["cpu_ticks"] - a["cpu_ticks"]) / 10_000_000       # 100ns ticks to seconds
    return round(max(0.0, used / wall) * 100.0, 1)


# ── the box ────────────────────────────────────────────────────────────────

def system_runtime() -> dict[str, Any]:
    """What /system/node reads off /proc on Linux: total and available
    memory, the percent used, and uptime. {} off Windows."""
    if not IS_WINDOWS:
        return {}
    import ctypes
    from ctypes import wintypes
    out: dict[str, Any] = {}
    k32 = _k32()

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    ms = MEMORYSTATUSEX()
    ms.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if k32.GlobalMemoryStatusEx(ctypes.byref(ms)):
        total_mb = int(ms.ullTotalPhys) // (1024 * 1024)
        avail_mb = int(ms.ullAvailPhys) // (1024 * 1024)
        out["memory_mb_total"] = total_mb
        out["memory_mb_available"] = avail_mb
        if total_mb:
            out["memory_pct_used"] = round(100 * (total_mb - avail_mb) / total_mb, 1)
    k32.GetTickCount64.restype = ctypes.c_ulonglong
    out["uptime_seconds"] = int(k32.GetTickCount64() // 1000)
    return out
