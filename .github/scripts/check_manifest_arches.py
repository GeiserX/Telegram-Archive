"""Fail unless a pushed manifest list carries every promised architecture.

Reads the raw OCI index / Docker manifest list JSON on stdin (what
`docker buildx imagetools inspect --raw <tag>` prints) and exits nonzero
unless linux/amd64 and linux/arm64 are both present. 8.1.0 and everything
before it shipped amd64-only while the workflows stayed green - QEMU was
set up, `platforms:` was not (#337). Green must mean "both architectures
are actually in the registry", so the publish workflows read the manifest
back and gate on it.

Usage: docker buildx imagetools inspect --raw "$tag" | python3 check_manifest_arches.py "$tag"
"""

import json
import sys

REQUIRED = {"linux/amd64", "linux/arm64"}


def main() -> int:
    tag = sys.argv[1] if len(sys.argv) > 1 else "<image>"
    data = json.load(sys.stdin)
    archs = set()
    for m in data.get("manifests", []):
        p = m.get("platform") or {}
        # Provenance/SBOM attestations ride along as unknown/unknown.
        if p.get("os", "unknown") != "unknown":
            archs.add(p.get("os", "") + "/" + p.get("architecture", ""))
    print(tag + ": " + (", ".join(sorted(archs)) or "no platform manifests"))
    missing = sorted(REQUIRED - archs)
    if missing:
        print("::error::" + tag + " is missing " + ", ".join(missing))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
