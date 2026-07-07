#!/usr/bin/env bash
# Pre-mirror secret + hostname scan (pre-mirror-scrub R3).
# Scans the content that WOULD be published (the tracked working tree, minus the
# paths excluded from the public mirror) for secrets and internal yard
# hostnames/IPs. A non-zero exit gates the publish.
#
# Usage: scripts/publish/scan-public.sh
# Exit:  0 = clean, 1 = findings (publish must not proceed)
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

# Paths excluded from the public mirror (handled by the mirror mechanism).
EXCLUDES=(':!context' ':!manifests' ':!traefik/config/chat.toml' ':!.scratch')

fail=0
report() { echo "  ✗ $1"; fail=1; }

echo "== yard hostnames / private IPs =="
if git grep -nEI 'home\.mateysmarsh\.com|172\.20\.[0-9]+\.[0-9]+|172\.16\.[0-9]+\.[0-9]+' -- . "${EXCLUDES[@]}" ; then
  report "internal hostname or private IP found in a shipping file"
else
  echo "  ✓ none"
fi

echo "== likely secrets =="
# High-signal patterns only, to avoid noise. Placeholders (REPLACE_ME, example,
# sk-placeholder, change-me) are allowed.
if git grep -nEI \
    -e 'BEGIN [A-Z ]*PRIVATE KEY' \
    -e '(ghp|gho|ghs|github_pat)_[A-Za-z0-9_]{20,}' \
    -e 'AKIA[0-9A-Z]{16}' \
    -e 'xox[baprs]-[0-9A-Za-z-]{10,}' \
    -- . "${EXCLUDES[@]}" \
  | grep -viE 'REPLACE_ME|example|placeholder|change-me' ; then
  report "possible committed secret"
else
  echo "  ✓ none"
fi

echo "== committed .env (not .env.example) =="
if git ls-files | grep -E '(^|/)\.env$' ; then
  report "a real .env file is tracked"
else
  echo "  ✓ none"
fi

if [ "$fail" -ne 0 ]; then
  echo "SCAN FAILED — resolve the findings above before publishing." >&2
  exit 1
fi
echo "SCAN CLEAN."
