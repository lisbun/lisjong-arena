"""Lightweight process runtime measurements shared across Arena responsibilities."""

import platform


def peak_process_ram_bytes() -> int | None:
    """Return best-effort peak process resident memory in bytes.

    The measurement intentionally mirrors the historical Phase 6 helper:
    Windows uses ``GetProcessMemoryInfo`` and POSIX uses ``ru_maxrss`` with
    the platform-specific unit conversion. Unsupported or unavailable
    measurements return ``None`` rather than fabricating a value.
    """
    if platform.system() == "Windows":
        try:
            import ctypes
            from ctypes import wintypes

            class _Counters(ctypes.Structure):
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

            counters = _Counters()
            counters.cb = ctypes.sizeof(counters)
            get_process = ctypes.windll.kernel32.GetCurrentProcess
            get_process.restype = wintypes.HANDLE
            get_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
            get_memory_info.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(_Counters),
                wintypes.DWORD,
            ]
            get_memory_info.restype = wintypes.BOOL
            if not get_memory_info(
                get_process(),
                ctypes.byref(counters),
                counters.cb,
            ):
                return None
            return int(counters.PeakWorkingSetSize)
        except AttributeError, OSError:
            return None
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(usage if platform.system() == "Darwin" else usage * 1024)
    except ImportError, OSError:
        return None


__all__ = ["peak_process_ram_bytes"]
