"""Portable, explicit resource observations. Unsupported values are None, not zero."""
from __future__ import annotations

import os
import platform
import re
import subprocess
from pathlib import Path


def command_text(command):
    try:
        return subprocess.check_output(command, text=True, timeout=5, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def memory_info():
    try:
        import psutil

        value = psutil.virtual_memory()
        return {"physical_bytes": value.total, "available_bytes": value.available,
                "method": "psutil.virtual_memory", "available_is_estimate": False}
    except (ImportError, OSError, RuntimeError):
        pass
    if platform.system() == "Linux":
        try:
            fields = {line.split(":")[0]: int(line.split()[1]) * 1024 for line in Path("/proc/meminfo").read_text().splitlines()}
            return {"physical_bytes": fields.get("MemTotal"), "available_bytes": fields.get("MemAvailable"),
                    "method": "proc_meminfo", "available_is_estimate": False}
        except (OSError, ValueError, IndexError):
            pass
    if platform.system() == "Darwin":
        total, vm = command_text(["sysctl", "-n", "hw.memsize"]), command_text(["vm_stat"])
        size = re.search(r"page size of (\d+) bytes", vm or "")
        fields = {name: int(count) for name, count in re.findall(r"(Pages [^:]+):\s*(\d+)\.", vm or "")}
        required = ("Pages free", "Pages inactive", "Pages speculative")
        available = sum(fields[name] for name in required) * int(size[1]) if size and all(name in fields for name in required) else None
        return {"physical_bytes": int(total) if total and total.isdigit() else None, "available_bytes": available,
                "method": "sysctl/vm_stat free+inactive+speculative; conservative estimate",
                "available_is_estimate": True}
    return {"physical_bytes": None, "available_bytes": None, "method": "unsupported", "available_is_estimate": None}


def affinity():
    try:
        return sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return None


def process_rss_bytes(pid, *, tree=False):
    try:
        import psutil

        process = psutil.Process(pid)
        processes = [process, *process.children(recursive=True)] if tree else [process]
        return sum(item.memory_info().rss for item in processes)
    except ImportError:
        pass
    except Exception as error:  # noqa: BLE001 - diagnostic only
        # psutil's platform-specific ProcessLookup/AccessDenied exceptions.
        if type(error).__name__ in {"NoSuchProcess", "AccessDenied", "ZombieProcess"}:
            return None
        return None
    if platform.system() != "Linux":
        return None
    try:
        if not tree:
            lines = Path(f"/proc/{pid}/status").read_text().splitlines()
            return next(int(line.split()[1]) * 1024 for line in lines if line.startswith("VmRSS:"))
        children = {}
        for path in Path("/proc").iterdir():
            if path.name.isdigit():
                try:
                    parent = int((path / "stat").read_text().rsplit(")", 1)[1].split()[1])
                    children.setdefault(parent, []).append(int(path.name))
                except (OSError, ValueError, IndexError):
                    continue
        total, pending = 0, [pid]
        while pending:
            current = pending.pop()
            value = process_rss_bytes(current)
            if value is None and current == pid:
                return None
            total += value or 0  # vanished child, not an unsupported root metric
            pending.extend(children.get(current, []))
        return total
    except (OSError, ValueError, StopIteration):
        return None


def peak_rss_bytes():
    """Cumulative SELF high-water mark; macOS reports bytes, Linux reports KiB."""
    try:
        import resource
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if platform.system() not in {"Linux", "Darwin"}:
            return None
        return int(value * (1024 if platform.system() == "Linux" else 1))
    except (ImportError, ValueError, OSError):
        return None


def host_metrics():
    model, cpu_model = None, platform.processor() or None
    if platform.system() == "Darwin":
        model = command_text(["sysctl", "-n", "hw.model"])
        cpu_model = command_text(["sysctl", "-n", "machdep.cpu.brand_string"])
    elif platform.system() == "Linux":
        try:
            cpu_model = next(line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines() if line.startswith("model name"))
        except (OSError, StopIteration):
            pass
    return {"system": platform.system(), "architecture": platform.machine(), "platform": platform.platform(),
            "machine_model": model, "cpu_model": cpu_model,
            "logical_cpus": os.cpu_count(), "affinity": affinity(), "memory": memory_info(),
            "limitations": "Available RAM is time-varying; summed child RSS may double-count shared pages."}
