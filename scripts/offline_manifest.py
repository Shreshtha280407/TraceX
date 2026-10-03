"""Create a bundle provenance manifest; runtime secrets are never captured."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


def main():
    output, image = Path(sys.argv[1]), sys.argv[2]
    images = json.loads(subprocess.check_output(["docker", "image", "inspect", image, "postgres:16-alpine"]))
    geoip = json.loads(subprocess.check_output(["docker", "run", "--rm", "--network", "none", image,
        "python", "-m", "app.engine.geoip", "--dir", "/opt/tracex/geoip", "status"]))
    manifest = {"schema": "tracex-offline-bundle-v1",
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "dirty_diff_sha256": hashlib.sha256(subprocess.check_output(["git", "diff", "HEAD", "--binary"])).hexdigest(),
        "untracked_sha256": {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in
                              subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard"], text=True).splitlines()
                              if Path(name).is_file()},
        "images": [{"id": item["Id"], "tags": item["RepoTags"], "digests": item["RepoDigests"]} for item in images],
        "geoip": geoip, "research_dependencies": "excluded from runtime; deployed inference remains v2",
        "integrity": "SHA256SUMS verifies accidental corruption, not publisher authenticity",
        "licences": {"country": "DB-IP CC-BY-4.0; attribution https://db-ip.com",
                     "asn": "IPtoASN PDDL-1.0; attribution https://iptoasn.com",
                     "runtime": "package metadata and upstream licences shipped with locked wheels"}}
    with (output / "manifest.json").open("x") as handle:
        json.dump(manifest, handle, indent=2)


if __name__ == "__main__":
    main()
