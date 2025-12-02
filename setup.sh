#!/usr/bin/env bash
set -euo pipefail

# -------- helpers --------
say() { printf "\n\033[1m%s\033[0m\n" "$*"; }
die() { printf "\nERROR: %s\n" "$*" >&2; exit 1; }

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

# Prefer python3.9 if available; else python3
PY_BIN="${PY_BIN:-}"
if [[ -z "${PY_BIN}" ]]; then
  if command -v python3.9 >/dev/null 2>&1; then
    PY_BIN="python3.9"
  elif command -v python3 >/dev/null 2>&1; then
    PY_BIN="python3"
  elif command -v python >/dev/null 2>&1; then
    PY_BIN="python"
  else
    die "Python tidak ditemukan. Install Python 3.9+ dulu."
  fi
fi

say "Using Python: $($PY_BIN -V)"

# Check version >= 3.9
"$PY_BIN" - <<'PY' || exit 1
import sys
major, minor = sys.version_info[:2]
if (major, minor) < (3, 9):
    raise SystemExit(f"Python {major}.{minor} terlalu tua. Butuh Python >= 3.9")
print("Python OK:", sys.version)
PY

VENV_DIR="${VENV_DIR:-.venv}"

# Create venv if needed
if [[ ! -d "$VENV_DIR" ]]; then
  say "Creating venv in: $VENV_DIR"
  "$PY_BIN" -m venv "$VENV_DIR"
fi

# Activate venv
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

say "Upgrading pip/setuptools/wheel..."
python -m pip install --upgrade pip setuptools wheel

if [[ -f "requirements.txt" ]]; then
  say "Installing dependencies from requirements.txt..."
  python -m pip install -r requirements.txt
else
  say "requirements.txt tidak ditemukan — skip dependency install."
  say "Kalau project butuh dependency, buat requirements.txt lalu jalankan lagi."
fi

# Env template handling
if [[ ! -f ".env" && -f ".env.template" ]]; then
  say "Creating .env from .env.template..."
  cp ".env.template" ".env"
fi

say "Done."
echo "Run:"
echo "  source $VENV_DIR/bin/activate"
echo "  python main.py"