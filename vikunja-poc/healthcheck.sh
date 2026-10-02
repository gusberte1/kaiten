#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
set -a
source "$HERE/.env"
set +a
python3 - "$VIKUNJA_BIND_IP" <<'PY'
import sys
from urllib.request import urlopen

url = f"http://{sys.argv[1]}:3457/api/v2/info"
with urlopen(url, timeout=10) as response:
    print(f"{url} -> HTTP {response.status}")
PY
