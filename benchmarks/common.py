#!/usr/bin/env python3
# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Shared helpers for PaveDB benchmark scripts.
"""
from __future__ import annotations

import datetime
import getpass
import os
import platform
import socket
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx


def local_hw() -> str:
    """Return a one-line string describing the local machine's CPU/RAM."""
    cpu = platform.processor() or platform.machine()
    cores = os.cpu_count()
    ram = None
    try:
        if sys.platform == "linux":
            with open("/proc/meminfo", encoding="ascii") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        ram = round(int(line.split()[1]) / 1_000_000, 1)
                        break
        elif sys.platform == "darwin":
            import subprocess
            out = subprocess.check_output(
                ["sysctl", "-n", "hw.memsize"], text=True, timeout=2
            )
            ram = round(int(out.strip()) / 1_000_000_000, 1)
        elif sys.platform == "win32":
            import subprocess
            out = subprocess.check_output(
                ["wmic", "ComputerSystem", "get", "TotalPhysicalMemory", "/Value"],
                text=True, timeout=5,
            )
            for line in out.splitlines():
                if "TotalPhysicalMemory=" in line:
                    ram = round(int(line.split("=")[1].strip()) / 1_000_000_000, 1)
                    break
    except Exception:
        pass
    ram_str = f"{ram} GB" if ram is not None else "?"
    return f"{cpu}, {cores} cores, {ram_str} RAM"


def host_load() -> str | None:
    """Return a one-line load-average summary, or None where unavailable.

    Docker does not namespace /proc/loadavg, so in CI this is the queue of
    the whole host, neighbours included. It is the one number that separates
    a latency gate the server made red from one a job sharing the box made
    red, and the saved report is the only place a later reader can find it.
    """
    try:
        one, five, fifteen = os.getloadavg()
    except (AttributeError, OSError):  # not on this platform
        return None
    cores = os.cpu_count() or 0
    per_core = f" ({one / cores:.2f}/core over {cores})" if cores else ""
    return f"1m {one:.2f}{per_core}, 5m {five:.2f}, 15m {fifteen:.2f}"


async def print_run_header(
    client: "httpx.AsyncClient",
    base_url: str,
    bench_name: str,
) -> None:
    """Print a benchmark run header with timestamp, runner, and server info."""
    ts = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    runner = f"{getpass.getuser()}@{socket.gethostname()}"

    srv_line = base_url
    srv_hw = ""
    try:
        r = await client.get("/health/metrics", timeout=5.0)
        if r.status_code < 400:
            d = r.json()
            ver = d.get("version", "")
            store = d.get("vector_store", "")
            if ver:
                srv_line += f"  v{ver}"
            if store:
                srv_line += f"  store={store}"
            hw_parts = [
                d.get("hw_cpu", ""),
                f"{d['hw_cores']} cores" if d.get("hw_cores") else "",
                f"{d['hw_ram_gb']} GB RAM" if d.get("hw_ram_gb") else "",
                f"({d['hw_os']})" if d.get("hw_os") else "",
            ]
            srv_hw = "  ".join(p for p in hw_parts if p)
    except Exception:
        pass

    is_remote = not any(
        h in base_url for h in ("localhost", "127.0.0.1", "::1")
    )
    print(f"==> Benchmark: {bench_name}")
    print(f"    ts        : {ts}")
    print(f"    runner    : {runner}  |  {local_hw()}")
    load = host_load()
    if load:
        print(f"    host load : {load}")
    print(f"    server    : {srv_line}")
    if srv_hw:
        label = "  [bench client ≠ server]" if is_remote else ""
        print(f"    server hw : {srv_hw}{label}")
    print()
