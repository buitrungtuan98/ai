"""Host vitals (CPU load + memory) read straight from the kernel.

Deliberately **stdlib only**: `psutil` would be a new runtime dependency for two numbers Linux already
publishes, and this box is the one deployment target (YAGNI). Every reader fails soft to `None`, so a
non-Linux dev machine — or a container without `/proc` — renders an honest "—" instead of raising.

Disk usage is NOT here: `main._system_health` already owns that single definition (DRY).

CPU is reported as *load average*, not instantaneous percent. On a box whose whole job is one nice-19
ffmpeg render, "is the machine saturated?" is a question about queued work over time, and load per
core answers it without sampling twice and holding state.
"""
from __future__ import annotations

import os

_MEMINFO = "/proc/meminfo"


def cpu_cores() -> int:
    """Cores available to THIS process (respects a container's cpuset), floor of 1."""
    try:
        return len(os.sched_getaffinity(0)) or 1     # Linux: what the cgroup actually allows
    except AttributeError:
        return os.cpu_count() or 1


def load_average() -> float | None:
    """1-minute load average, or None where the platform has none (e.g. Windows)."""
    try:
        return round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        return None


def memory() -> tuple[int, int] | None:
    """(used_mb, total_mb) from /proc/meminfo, or None when it is unreadable.

    Used = Total − *Available* (not Total − Free): page cache is reclaimable, and counting it as used
    would show this box at 95% memory while it is perfectly healthy.
    """
    try:
        with open(_MEMINFO, encoding="utf-8") as fh:
            fields = {}
            for line in fh:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    fields[key] = int(rest.split()[0])          # kB
                    if len(fields) == 2:
                        break
        total_kb, avail_kb = fields["MemTotal"], fields["MemAvailable"]
    except (OSError, KeyError, ValueError, IndexError):
        return None
    if total_kb <= 0:
        return None
    return (total_kb - avail_kb) // 1024, total_kb // 1024


def snapshot() -> dict:
    """CPU + memory vitals for the dashboard. Keys are always present; values may be None.

    `load_pct` is load-per-core as a percentage, capped at 100 for the bar — above one job per core
    the box is queueing work, and how far above stops mattering to the operator.
    """
    cores = cpu_cores()
    load = load_average()
    mem = memory()
    return {
        "cores": cores,
        "load": load,
        "load_pct": min(100, round(load / cores * 100)) if load is not None else None,
        "ram_used_mb": mem[0] if mem else None,
        "ram_total_mb": mem[1] if mem else None,
        "ram_pct": round(mem[0] / mem[1] * 100) if mem and mem[1] else None,
    }
