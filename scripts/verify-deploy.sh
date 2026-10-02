#!/bin/bash
# Post-deploy verification for news-pipeline (p-rocmon.vercel.app)
# Run after every Vercel deploy to verify the live site works.
# Usage: ./verify-deploy.sh [deployment-url]
# If no URL given, checks the production alias.

set -euo pipefail

URL="${1:-https://p-rocmon.vercel.app}"
FAIL=0

check() {
  local name="$1" url="$2" expect="$3"
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 "$url" 2>&1)
  if [ "$code" = "$expect" ]; then
    echo "OK: $name ($url) -> $code"
  else
    echo "FAIL: $name ($url) -> got $code, expected $expect"
    FAIL=1
  fi
}

echo "Verifying $URL..."
echo ""

check "Homepage (auth-gated)" "$URL/" "401"
check "Map page" "$URL/map" "200"
check "Health" "$URL/healthz" "200"
check "Map events API" "$URL/api/globe/events?limit=1" "200"
check "Map stories API" "$URL/api/map/stories" "200"

echo ""
if [ $FAIL -eq 0 ]; then
  echo "All checks passed."
else
  echo "Some checks FAILED."
  exit 1
fi
