#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

if [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python" ]; then
    exec "$VIRTUAL_ENV/bin/python" -m src.main
elif [ -x "venv/bin/python" ]; then
    exec "venv/bin/python" -m src.main
else
    exec python -m src.main
fi
