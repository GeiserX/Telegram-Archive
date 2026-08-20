#!/usr/bin/env python3
"""Docker HEALTHCHECK for the backup container (9t6.8.10).

The scheduler touches a heartbeat file while its event loop is responsive
(src/scheduler.py). A dead process, a wedged loop, or an asyncio deadlock
all stop the touches — exactly the "dead archiver looks healthy" failure
this check exists to expose. Exit 0 = healthy, 1 = unhealthy.
"""

import os
import sys
import time

DEFAULT_HEARTBEAT_FILE = "/tmp/telegram-archive.heartbeat"
DEFAULT_MAX_AGE_SECONDS = 180  # three missed 30s beats + slack


def main() -> int:
    path = os.getenv("HEARTBEAT_FILE", DEFAULT_HEARTBEAT_FILE)
    max_age = int(os.getenv("HEARTBEAT_MAX_AGE_SECONDS", str(DEFAULT_MAX_AGE_SECONDS)))
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return 1  # never written: the scheduler has not proven liveness
    return 0 if age < max_age else 1


if __name__ == "__main__":
    sys.exit(main())
