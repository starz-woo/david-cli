#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python3}"
export PYTHONDONTWRITEBYTECODE=1
export TZ=UTC
exec "$PYTHON" -m unittest discover -s tests -v "$@"
