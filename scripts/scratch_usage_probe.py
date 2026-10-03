"""Read-only metadata sampling of benchmark scratch, never raw evidence bytes."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from pathlib import Path


def scratch_usage(root):
    root = Path(root)
    if root.is_symlink():
        raise ValueError("Scratch root symlinks are not allowed")
    scratch_bytes = spill_bytes = files = 0
    locations = [root / ".staging", *(case / ".work" for case in root.iterdir() if case.is_dir())]
    for location in locations:
        if location.is_symlink():
            raise ValueError("Scratch directory symlinks are not allowed")
        for directory, names, filenames in os.walk(location, followlinks=False):
            base = Path(directory)
            if any((base / name).is_symlink() for name in names + filenames):
                raise ValueError("Scratch artifact symlinks are not allowed")
            for name in filenames:
                try:
                    size = (base / name).stat().st_size
                except FileNotFoundError:
                    continue  # normal stage cleanup between listing and stat
                scratch_bytes += size
                files += 1
                if "spill" in base.relative_to(location).parts:
                    spill_bytes += size
    return {"scratch_logical_bytes": scratch_bytes, "spill_logical_bytes": spill_bytes,
            "scratch_files": files, "filesystem_free_bytes": shutil.disk_usage(root).free}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--until-result", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=7200)
    parser.add_argument("--interval", type=float, default=5)
    args = parser.parse_args()
    if args.interval <= 0 or args.timeout <= 0:
        parser.error("positive sampling interval and timeout required")
    started = time.monotonic()
    with args.output.open("x") as handle:
        while True:
            handle.write(json.dumps({"elapsed_from_probe_start": time.monotonic() - started,
                                     "sampling_interval_seconds": args.interval,
                                     "scope": "logical .work/.staging sizes, not physical exclusive disk allocation",
                                     **scratch_usage(args.root)}) + "\n")
            handle.flush()
            if args.until_result.exists():
                return 0
            if time.monotonic() - started >= args.timeout:
                raise TimeoutError("scratch observer deadline; benchmark is not cancelled")
            time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
