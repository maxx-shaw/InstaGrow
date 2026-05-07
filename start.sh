#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
VENV_DIR="$SCRIPT_DIR/.venv"

echo "==> InstaGrow"

# Create virtualenv if needed
if [ ! -d "$VENV_DIR" ]; then
  echo "==> Creating virtualenv..."
  python3 -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

# Install dependencies
echo "==> Installing dependencies..."
pip install -q -r "$BACKEND_DIR/requirements.txt"

echo ""
echo "==> Starting InstaGrow at http://localhost:8000"
echo ""

cd "$BACKEND_DIR"
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
