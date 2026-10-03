"""Retire explicitly approved obsolete review payloads, keeping all metadata.

Only review releases 02--05 are eligible. Requires a verified later copied
install. Writes a checksum archive before unlinking payloads; never removes
directories, .env files, Docker images/volumes or investigation data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OBSOLETE_RELEASES = {"02", "03", "04", "05"}


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def checked_file(path, parent):
    if path.is_symlink() or not path.is_file() or path.resolve().parent != parent.resolve():
        raise ValueError("Unsafe/missing exact bundle file")
    return path


def checksums(directory):
    pairs = {}
    for line in (directory / "SHA256SUMS").read_text().splitlines():
        checksum, name = line.split(maxsplit=1)
        name = name.lstrip(" *")
        if Path(name).name != name or len(checksum) != 64:
            raise ValueError("Unsafe bundle checksum manifest")
        path = checked_file(directory / name, directory)
        if digest(path) != checksum:
            raise ValueError("Bundle checksum mismatch; nothing retired")
        pairs[name] = checksum
    return pairs


def payloads(installation, current):
    for directory in (installation, current):
        if directory.is_symlink() or directory.resolve().parent != REPO / "var":
            raise ValueError("Only explicit immediate review-install children are eligible")
    result = json.loads((installation / "installation_result.json").read_text())
    final = json.loads((current / "installation_result.json").read_text())
    if result.get("status") != "pass" or final.get("status") != "pass":
        raise ValueError("Both copied installations must have passed")
    release = installation.name.removeprefix("offline-install-20261003-release-")
    if release not in OBSOLETE_RELEASES or current.name != "offline-install-20261003-release-06":
        raise ValueError("Only approved obsolete releases 02--05 with retained release06")
    manifest = json.loads((installation / "manifest.json").read_text())
    final_manifest = json.loads((current / "manifest.json").read_text())
    expected_tag = "tracex-review-appliance:20261003-release-" + release
    if manifest["images"][0]["tags"] != [expected_tag] or manifest["images"][0]["id"] == final_manifest["images"][0]["id"]:
        raise ValueError("Not the exact obsolete review image")
    source = Path(result["source"])
    if source.is_symlink() or source.resolve().parent != REPO / "dist" or not source.name.startswith("tracex-offline-"):
        raise ValueError("Unsafe bundle source")
    original, copy = checksums(source), checksums(installation)
    if original != copy:
        raise ValueError("Copied bundle differs from archived original")
    compressed = checked_file(source.with_name(source.name + ".tar.gz"), REPO / "dist")
    members = {}
    with tarfile.open(compressed, "r:gz") as archive:
        for member in archive:
            parts = Path(member.name).parts
            if not parts or parts[0] != source.name or member.issym() or member.islnk() or ".." in parts:
                raise ValueError("Unsafe compressed bundle member")
            if member.isdir():
                continue
            if not member.isfile() or len(parts) != 2:
                raise ValueError("Unexpected compressed bundle member")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("Missing compressed member")
            with stream:
                members[parts[1]] = hashlib.file_digest(stream, "sha256").hexdigest()
    expected = {**original, "SHA256SUMS": digest(source / "SHA256SUMS")}
    if members != expected:
        raise ValueError("Compressed bundle does not match preserved checksums")
    targets = [source / "images.tar", installation / "images.tar", compressed]
    return [{"path": str(path.relative_to(REPO)), "bytes": path.stat().st_size, "sha256": digest(path)} for path in targets]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installation", type=Path, action="append", required=True)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--delete", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    artifacts = [item for installation in args.installation for item in payloads(installation.absolute(), args.current.absolute())]
    if len({item["path"] for item in artifacts}) != len(artifacts):
        raise ValueError("Duplicate targets")
    report = {"status": "verified", "deletion_requested": args.delete, "artifacts": artifacts,
              "logical_bytes": sum(item["bytes"] for item in artifacts),
              "disk_before": shutil.disk_usage(REPO)._asdict(),
              "retained": "release06 complete bundle/copy; all old manifests, checksums, installation logs/reports, .env; all Docker images/volumes and real data",
              "recoverable": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as handle:
        json.dump(report, handle, indent=2)
    if args.delete:
        for item in artifacts:
            (REPO / item["path"]).unlink()
        with args.output.with_suffix(".deleted.json").open("x") as handle:
            json.dump({"status": "deleted", "logical_bytes": report["logical_bytes"],
                       "disk_after": shutil.disk_usage(REPO)._asdict(), "recoverable": False}, handle, indent=2)
    print(json.dumps({"verified_payloads": len(artifacts), "bytes": report["logical_bytes"], "deleted": args.delete}))


if __name__ == "__main__":
    main()
