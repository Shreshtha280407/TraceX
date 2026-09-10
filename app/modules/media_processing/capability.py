"""Safe, best-effort local GPU/CPU and codec-tooling capability detection.

GPU absence is the normal case and must never fail the pipeline: every
check here is wrapped so a missing binary, a driver-less `nvidia-smi`, or a
timeout all collapse to "not available" rather than raising. This module
never installs or requires CUDA, never logs raw subprocess output, and
never claims GPU acceleration is in use -- Phase 1 has no GPU-backed
analysis code to accelerate in the first place (see
`docs/architecture/media-processing-v1.md`).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

_NVIDIA_SMI_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class CapabilityReport:
    """A safe, local capability summary -- never a promise of acceleration."""

    cpu_only: bool
    nvidia_smi_available: bool
    gpu_visible: bool
    gpu_name: str | None
    gpu_memory_mb: int | None
    ffmpeg_available: bool
    ffprobe_available: bool


def _binary_available(name: str) -> bool:
    return shutil.which(name) is not None


def _query_gpu() -> tuple[bool, str | None, int | None]:
    """Best-effort `nvidia-smi` query. Never raises; absence is normal."""
    if not _binary_available("nvidia-smi"):
        return False, None, None
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=_NVIDIA_SMI_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, None, None

    if completed.returncode != 0:
        return False, None, None

    first_line = completed.stdout.strip().splitlines()[0] if completed.stdout.strip() else ""
    if "," not in first_line:
        return False, None, None

    name_part, _, memory_part = first_line.partition(",")
    name = name_part.strip() or None
    try:
        memory_mb = int(float(memory_part.strip()))
    except ValueError:
        memory_mb = None
    return True, name, memory_mb


def detect_capability() -> CapabilityReport:
    """Detect local capability without requiring, or depending on, a GPU."""
    ffmpeg_available = _binary_available("ffmpeg")
    ffprobe_available = _binary_available("ffprobe")
    nvidia_smi_available = _binary_available("nvidia-smi")
    gpu_visible, gpu_name, gpu_memory_mb = _query_gpu()
    return CapabilityReport(
        cpu_only=not gpu_visible,
        nvidia_smi_available=nvidia_smi_available,
        gpu_visible=gpu_visible,
        gpu_name=gpu_name,
        gpu_memory_mb=gpu_memory_mb,
        ffmpeg_available=ffmpeg_available,
        ffprobe_available=ffprobe_available,
    )
