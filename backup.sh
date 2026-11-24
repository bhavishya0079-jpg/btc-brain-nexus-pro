#!/usr/bin/env bash
# Create a DB backup via the running API. Usage: NEXUS_URL=http://127.0.0.1:8000 NEXUS_USER=admin NEXUS_PASS=... scripts/backup.sh
set -e
U="${NEXUS_URL:-http://127.0.0.1:8000}"
T=$(curl -s -X POST "$U/api/auth/login" -H 'content-type: application/json' -d "{\"username\":\"${NEXUS_USER:-admin}\",\"password\":\"$NEXUS_PASS\"}" | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
curl -s -X POST "$U/api/backup" -H "authorization: Bearer $T"; echo
