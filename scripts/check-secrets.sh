#!/usr/bin/env bash
#
# Pre-commit secret guard — blocks staged Solcast API keys (and similar).
#
# Scans the staged content (the git index) for things that look like a Solcast
# API key or a populated `api_key` assignment, and aborts the commit if any are
# found. Redacted placeholders (e.g. "REDACTED") are ignored.
#
# Install (once per clone):
#   ln -sf ../../scripts/check-secrets.sh .git/hooks/pre-commit
#   # or:  cp scripts/check-secrets.sh .git/hooks/pre-commit
#   chmod +x .git/hooks/pre-commit scripts/check-secrets.sh
#
# Run manually against the current index:
#   ./scripts/check-secrets.sh
#
# Bypass a false positive (use sparingly):
#   git commit --no-verify
#
set -uo pipefail

# Patterns considered secret-like in staged content.
#  1. Solcast API key: a short prefix, an underscore, then a long token
#     (the leaked example was `huwv_` + 27 alphanumerics).
#  2. A generic `api_key` assignment whose value is long enough to be real.
PATTERNS=(
  '[A-Za-z0-9]{4}_[A-Za-z0-9]{24,40}'
  'api_key"?[[:space:]]*[:=][[:space:]]*"?[A-Za-z0-9_-]{16,}'
)

# Redact secret-like tokens before echoing a match back to the terminal, so the
# guard never re-leaks the value it just caught.
mask() {
  sed -E \
    -e 's/[A-Za-z0-9]{4}_[A-Za-z0-9]{12,}/***REDACTED***/g' \
    -e 's/[A-Za-z0-9]{20,}/***REDACTED***/g'
}

hits=""
for re in "${PATTERNS[@]}"; do
  # --cached scans the index (staged adds + modifications); -I skips binaries.
  m=$(git grep --cached -nIE "$re" -- . 2>/dev/null | grep -vi 'REDACTED' || true)
  [ -n "$m" ] && hits+="$m"$'\n'
done

# Strip newlines to test for any real content.
if [ -n "${hits//[$'\n']/}" ]; then
  {
    echo "✖ Potential secret detected in staged changes — commit blocked."
    echo
    printf '%s\n' "$hits" | mask | sort -u | sed '/^$/d'
    echo
    echo "Rotate the key if it is real, remove it from the staged change, and"
    echo "retry. If this is a genuine false positive: git commit --no-verify"
  } >&2
  exit 1
fi

exit 0
