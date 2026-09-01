"""Read this process's own block-I/O and memory accounting from its cgroup.

Pairs with cgroup.py (CPU). Together they let a run attribute wall-clock time
to three sources: computing, waiting on the CPU limiter, or waiting on disk.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

CG2 = "/sys/fs/cgroup"


def _read(path: str) -> str | None:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


@dataclass(frozen=True)
class IoStat:
    wall: float
    rbytes: int
    wbytes: int
    rios: int
    wios: int
    # Pressure Stall Information: microseconds this cgroup spent stalled on IO.
    # The most direct "was I waiting on disk" signal the kernel offers.
    io_stall_us: int
    mem_stall_us: int
    mem_current: int
    mem_peak: int


def _sum_io_stat() -> tuple[int, int, int, int]:
    """cgroup v2 io.stat is per-device; sum across devices."""
    raw = _read(f"{CG2}/io.stat")
    r = w = ri = wi = 0
    if not raw:
        return r, w, ri, wi
    for line in raw.splitlines():
        for field in line.split()[1:]:
            if "=" not in field:
                continue
            k, v = field.split("=", 1)
            try:
                n = int(v)
            except ValueError:
                continue
            if k == "rbytes":
                r += n
            elif k == "wbytes":
                w += n
            elif k == "rios":
                ri += n
            elif k == "wios":
                wi += n
    return r, w, ri, wi


def _pressure_total(name: str) -> int:
    """Parse 'some ... total=N' from a PSI file. Returns microseconds."""
    raw = _read(f"{CG2}/{name}")
    if not raw:
        return 0
    for line in raw.splitlines():
        if line.startswith("some"):
            for field in line.split():
                if field.startswith("total="):
                    try:
                        return int(field.split("=", 1)[1])
                    except ValueError:
                        return 0
    return 0


def sample() -> IoStat:
    r, w, ri, wi = _sum_io_stat()
    return IoStat(
        wall=time.monotonic(),
        rbytes=r,
        wbytes=w,
        rios=ri,
        wios=wi,
        io_stall_us=_pressure_total("io.pressure"),
        mem_stall_us=_pressure_total("memory.pressure"),
        mem_current=int(_read(f"{CG2}/memory.current") or 0),
        mem_peak=int(_read(f"{CG2}/memory.peak") or 0),
    )


@dataclass(frozen=True)
class IoDelta:
    elapsed: float
    read_mb: float
    write_mb: float
    read_iops: float
    write_iops: float
    io_stall_frac: float   # fraction of wall time stalled on IO
    mem_stall_frac: float
    mem_peak_gib: float

    @property
    def total_mb(self) -> float:
        return self.read_mb + self.write_mb


def delta(before: IoStat, after: IoStat) -> IoDelta:
    elapsed = max(after.wall - before.wall, 1e-9)
    return IoDelta(
        elapsed=elapsed,
        read_mb=(after.rbytes - before.rbytes) / 2**20,
        write_mb=(after.wbytes - before.wbytes) / 2**20,
        read_iops=(after.rios - before.rios) / elapsed,
        write_iops=(after.wios - before.wios) / elapsed,
        io_stall_frac=(after.io_stall_us - before.io_stall_us) / 1e6 / elapsed,
        mem_stall_frac=(after.mem_stall_us - before.mem_stall_us) / 1e6 / elapsed,
        mem_peak_gib=after.mem_peak / 2**30,
    )


def memory_limit_bytes() -> int | None:
    raw = _read(f"{CG2}/memory.max")
    if raw and raw != "max":
        return int(raw)
    return None


def psi_available() -> bool:
    """PSI requires kernel support and cgroup v2; report it rather than
    silently showing zeros."""
    return os.path.exists(f"{CG2}/io.pressure")
