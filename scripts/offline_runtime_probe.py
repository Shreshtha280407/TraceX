"""Evidence for internal-only copied-appliance runtime, not a host Wi-Fi shutdown."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--context", default="default")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    docker = ["docker", "--context", args.context]
    compose = [*docker, "compose", "-p", args.project, "-f", str(args.compose)]
    report = {"scope": "all appliance containers on internal-only networks; host network remains unchanged",
              "services": {}, "status": "failed"}
    try:
        errors = []
        for service in ("api", "worker", "postgres"):
            identity = subprocess.check_output([*compose, "ps", "-q", service], text=True).strip()
            if not identity:
                raise ValueError(f"missing service {service}")
            inspected = json.loads(subprocess.check_output([*docker, "inspect", identity]))[0]
            networks = inspected["NetworkSettings"]["Networks"]
            policies = {name: json.loads(subprocess.check_output([*docker, "network", "inspect", network["NetworkID"]]))[0]["Internal"]
                        for name, network in networks.items()}
            routes = subprocess.check_output([*docker, "exec", identity, "cat", "/proc/net/route"], text=True)
            default_route = any(line.split()[1] == "00000000" for line in routes.splitlines()[1:] if line.strip())
            result = {"image_id": inspected["Image"], "networks_internal": policies,
                      "ipv4_routes": routes, "default_ipv4_route": default_route}
            if not policies or not all(policies.values()) or default_route:
                errors.append(f"{service}: routable external network")
            if service != "postgres":
                code = ('import json,socket; result={}; '
                        's=socket.socket(); s.settimeout(3); result["public_ipv4_connect_errno"]=s.connect_ex(("1.1.1.1",443)); s.close(); '
                        's=socket.socket(socket.AF_INET6); s.settimeout(3); result["public_ipv6_connect_errno"]=s.connect_ex(("2606:4700:4700::1111",443)); s.close(); '
                        'print(json.dumps(result))')
                connectivity = json.loads(subprocess.check_output([*docker, "exec", identity, "python", "-c", code], text=True))
                result["connectivity"] = connectivity
                if any(value == 0 for value in connectivity.values()):
                    errors.append(f"{service}: public IP reachable")
            report["services"][service] = result
        report.update(status="pass" if not errors else "failed", errors=errors)
    except Exception as error:  # noqa: BLE001 - preserve offline failures
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as handle:
            json.dump(report, handle, indent=2)
    print(json.dumps(report, indent=2))
    return int(report["status"] != "pass")


if __name__ == "__main__":
    raise SystemExit(main())
