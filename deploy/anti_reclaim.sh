#!/bin/bash
# ==============================================================================
# Oracle Cloud Always Free "Anti-Reclamation" Professional Launcher
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_SCRIPT="$SCRIPT_DIR/anti_reclaim.py"

if [ -f "$PYTHON_SCRIPT" ]; then
    exec python3 "$PYTHON_SCRIPT"
else
    echo "[-] anti_reclaim.py not found, exiting."
    exit 1
fi
