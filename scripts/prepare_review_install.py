"""Copy a checksummed bundle and install an isolated, uniquely named review stack."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path


def review_environment(template, values):
    """Preserve review secrets while using actual configuration keys."""
    lines, seen = [], set()
    for line in template.splitlines():
        key = line.split("=", 1)[0]
        if key == "TRACEX_ML_FINDINGS_ENABLED":
            continue  # obsolete example key; app.config never consumed it
        seen.add(key)
        lines.append(f"{key}={values[key]}" if key in values else line)
    lines.extend(f"{key}={value}" for key, value in values.items() if key not in seen)
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project", default="tracex-review-final")
    parser.add_argument("--existing-env", type=Path, help="Preserve review-stack secrets during a verified image upgrade")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    destination = args.output.resolve()
    if destination.parent != root / "var" or not destination.name.startswith("offline-install-"):
        raise ValueError("Only a new explicitly named var/offline-install-* directory is allowed")
    if not args.project.startswith("tracex-review-"):
        raise ValueError("Use a unique review project, never the user's existing tracex stack")
    source = args.bundle.resolve(strict=True)
    if source.parent != root / "dist" or not (source / "SHA256SUMS").is_file():
        raise ValueError("Expected a locally built checksummed bundle")
    shutil.copytree(source, destination)  # exclusive; never overwrites an installation
    values = {"TRACEX_SECRET_KEY": secrets.token_hex(32), "TRACEX_DB_PASSWORD": secrets.token_hex(18),
              "TRACEX_PORT": "8770", "TRACEX_UI_INTERNAL": "true", "TRACEX_WORKERS": "4",
              "TRACEX_MEMORY_BUDGET_MB": "5120", "TRACEX_MAX_UPLOAD_BYTES": str(64 << 30),
              "TRACEX_LEASE_SECONDS": "600", "TRACEX_ML_FINDINGS": "1", "TRACEX_ML_REVIEW_BUDGET": "0.01"}
    template = destination / ".env.example"
    if args.existing_env:
        prior_env = args.existing_env.resolve(strict=True)
        if prior_env.name != ".env" or prior_env.parent.parent != root / "var" or not prior_env.parent.name.startswith("offline-install-"):
            raise ValueError("Only a previously generated review-install .env may be reused")
        template = prior_env
        values.pop("TRACEX_SECRET_KEY")
        values.pop("TRACEX_DB_PASSWORD")
        example = dict(line.split("=", 1) for line in (destination / ".env.example").read_text().splitlines()
                       if "=" in line and not line.startswith("#"))
        values["TRACEX_IMAGE"] = example["TRACEX_IMAGE"]
    env_path = destination / ".env"
    descriptor = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(review_environment(template.read_text(), values))
    manifest = json.loads((source / "manifest.json").read_text())
    image = manifest["images"][0]["tags"][0]
    prior = subprocess.run(["docker", "--context", "default", "image", "inspect", image],
                           capture_output=True, check=False)
    report = {"status": "failed", "source": str(source), "copy": str(destination), "project": args.project,
              "image_preexisting_in_target_engine": prior.returncode == 0, "expected_images": manifest["images"],
              "scope": "native Linux; host network unchanged; compose runtime networks internal; no registry pulls"}
    started = time.monotonic()
    try:
        result = subprocess.run(["sh", str(destination / "install.sh")], cwd=destination,
            env={**os.environ, "DOCKER_CONTEXT": "default", "COMPOSE_PROJECT_NAME": args.project},
            capture_output=True, text=True, check=False, timeout=600)
        (destination / "installation.log").write_text(result.stdout + result.stderr)
        report.update(status="pass" if result.returncode == 0 else "failed", exit_code=result.returncode)
    except Exception as error:  # noqa: BLE001 - installation evidence retained
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        report["seconds"] = time.monotonic() - started
        with (destination / "installation_result.json").open("x") as handle:
            json.dump(report, handle, indent=2)
        print(json.dumps(report, indent=2))
    return int(report["status"] != "pass")


if __name__ == "__main__":
    raise SystemExit(main())
