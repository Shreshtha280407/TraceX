"""Authenticated, bounded full findings export; credentials never enter argv/logs."""
import argparse
import getpass
import urllib.request
from pathlib import Path

from scripts.appliance_acceptance import request


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    token = request(args.base, "/auth/login", data={"display_name": args.user, "password": getpass.getpass()})["token"]
    req = urllib.request.Request(args.base + "/v1/cases/" + args.case + "/findings/export.ndjson", headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=300) as response, args.output.open("xb") as stream:
        while chunk := response.read(1 << 20):
            stream.write(chunk)
    print(str(args.output))


if __name__ == "__main__":
    main()
