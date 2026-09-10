"""Scenario 16: GPU absence is safe -- capability detection never raises."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from app.modules.media_processing import capability as capability_module


def test_detect_capability_never_raises_when_nvidia_smi_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(capability_module, "_binary_available", lambda _name: False)
    report = capability_module.detect_capability()
    assert report.cpu_only is True
    assert report.gpu_visible is False
    assert report.gpu_name is None
    assert report.gpu_memory_mb is None
    assert report.nvidia_smi_available is False


def test_detect_capability_never_raises_when_nvidia_smi_fails_to_communicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(capability_module, "_binary_available", lambda _name: True)
    monkeypatch.setattr(
        capability_module.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="no driver"
        ),
    )
    report = capability_module.detect_capability()
    assert report.cpu_only is True
    assert report.gpu_visible is False


def test_detect_capability_never_raises_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capability_module, "_binary_available", lambda _name: True)

    def _raise_timeout(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=5)

    monkeypatch.setattr(capability_module.subprocess, "run", _raise_timeout)
    report = capability_module.detect_capability()
    assert report.cpu_only is True


def test_detect_capability_never_raises_on_missing_binary_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(capability_module, "_binary_available", lambda _name: True)

    def _raise_oserror(*args: Any, **kwargs: Any) -> Any:
        raise OSError("no such file")

    monkeypatch.setattr(capability_module.subprocess, "run", _raise_oserror)
    report = capability_module.detect_capability()
    assert report.cpu_only is True


def test_detect_capability_parses_a_successful_gpu_report(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capability_module, "_binary_available", lambda _name: True)
    monkeypatch.setattr(
        capability_module.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="NVIDIA T4, 16384\n", stderr=""
        ),
    )
    report = capability_module.detect_capability()
    assert report.gpu_visible is True
    assert report.cpu_only is False
    assert report.gpu_name == "NVIDIA T4"
    assert report.gpu_memory_mb == 16384


def test_detect_capability_handles_unparseable_gpu_output_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(capability_module, "_binary_available", lambda _name: True)
    monkeypatch.setattr(
        capability_module.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="garbage\n", stderr=""
        ),
    )
    report = capability_module.detect_capability()
    assert report.gpu_visible is False


def test_detect_capability_reports_ffmpeg_ffprobe_availability_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_available(name: str) -> bool:
        return name == "ffmpeg"

    monkeypatch.setattr(capability_module, "_binary_available", _fake_available)
    monkeypatch.setattr(
        capability_module.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=""),
    )
    report = capability_module.detect_capability()
    assert report.ffmpeg_available is True
    assert report.ffprobe_available is False
