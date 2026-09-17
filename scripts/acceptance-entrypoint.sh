#!/bin/sh
# Wait for the API to become healthy, then run the acceptance suite once.
# Used as the entrypoint of the one-shot `verify` compose service.
set -e

python - <<'PY'
import os
import sys
import time
import urllib.request

url = os.environ.get("HEALTH_URL", "http://api:8000/health")
deadline = time.time() + 30
while True:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            if response.status == 200:
                print(f"api ready at {url}")
                break
    except OSError:
        pass
    if time.time() > deadline:
        print(f"api did not become ready at {url}", file=sys.stderr)
        sys.exit(1)
    time.sleep(0.5)
PY

exec pytest "$@"
