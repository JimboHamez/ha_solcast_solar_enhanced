#!/usr/bin/env bash

# Static-analysis / security scan for the custom component only.
#
# Runs bandit, semgrep, mypy and pip-audit. All file-scanning tools are
# scoped to the integration package under custom_components/ so the
# intentionally-unclean dirs (tests/, tools/, analysis/, scripts/, venvs)
# don't drown out real findings. See CLAUDE.md.
#
# Usage:
#   ./scan.sh                     # scan custom_components/solcast_solar_enhanced
#   ./scan.sh path/to/other/pkg   # scan a different target directory

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

# Target: default to the integration package, allow an override via $1.
TARGET="${1:-$SCRIPT_DIR/custom_components/solcast_solar_enhanced}"
if [ ! -d "$TARGET" ]; then
    echo "Error: target directory not found: $TARGET"
    exit 1
fi
echo "Scan target: $TARGET"

echo "=== 1. Setting up Virtual Environment ==="
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment in $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "=== 2. Ensuring Python Tools are Installed ==="
pip install --upgrade pip

TOOLS=(bandit semgrep mypy pip-audit)
for tool in "${TOOLS[@]}"; do
    if ! command -v "$tool" &> /dev/null; then
        echo "$tool not found. Installing..."
        pip install "$tool"
    else
        echo "$tool is already installed."
    fi
done

echo "=== 3. Running Scanners (scoped to $TARGET) ==="

# 1. Bandit: recurse the target package for common security issues.
echo "--> Running Bandit..."
bandit -r "$TARGET" || echo "Bandit found issues."

# 2. Semgrep: auto ruleset against the target package only.
echo "--> Running Semgrep..."
semgrep scan --config auto "$TARGET" || echo "Semgrep found issues."

# 3. Mypy: static type checking of the target package only.
#    HA and numpy are resolvable from this venv, so imports type-check.
echo "--> Running Mypy..."
mypy "$TARGET" || echo "Mypy found type errors."

# 4. Pip-audit: audits the project's declared dependency set (the test/HA
#    stack; manifest.json requirements is empty) for known-vulnerable
#    packages. pip-audit is dependency- not path-scoped by nature.
echo "--> Running Pip-audit..."
pip-audit -r "$SCRIPT_DIR/requirements_test.txt" || echo "Pip-audit found vulnerable packages."

echo "=== 4. Cleaning Up ==="
deactivate

echo "Done!"
