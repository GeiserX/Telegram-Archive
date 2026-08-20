#!/usr/bin/env python3
"""Docker HEALTHCHECK for the viewer container (9t6.8.10).

Probes the viewer's own /api/health and requires status "ok" — "degraded"
(database unreachable) counts as unhealthy, because a viewer that cannot
read the archive is not serving. Exit 0 = healthy, 1 = unhealthy.
"""

import json
import os
import sys
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8000/api/health"


def main() -> int:
    url = os.getenv("HEALTHCHECK_URL", DEFAULT_URL)
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            if response.status != 200:
                return 1
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return 1
    return 0 if payload.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
