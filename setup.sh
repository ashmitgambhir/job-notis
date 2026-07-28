#!/usr/bin/env bash
# One-command setup. Creates a venv, installs, seeds .env, initialises the DB.
set -euo pipefail

cd "$(dirname "$0")"

BOLD=$(tput bold 2>/dev/null || true); RESET=$(tput sgr0 2>/dev/null || true)
say() { printf "%s==>%s %s\n" "$BOLD" "$RESET" "$1"; }
die() { printf "Error: %s\n" "$1" >&2; exit 1; }

PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null || die "python3 not found"

version=$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
"$PYTHON" - <<'PY' || die "Python 3.9+ required (found $version)"
import sys
sys.exit(0 if sys.version_info >= (3, 9) else 1)
PY
say "Using $PYTHON ($version)"

if [ ! -d .venv ]; then
  say "Creating virtualenv"
  "$PYTHON" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

say "Installing dependencies"
pip install --quiet --upgrade pip setuptools wheel

EXTRAS=""
if [ "${WITH_EMBEDDINGS:-ask}" = "ask" ] && [ -t 0 ]; then
  printf "Install local semantic matching? Much better matching, ~2GB download. [Y/n] "
  read -r reply
  case "$reply" in [Nn]*) EXTRAS="" ;; *) EXTRAS="[embeddings]" ;; esac
elif [ "${WITH_EMBEDDINGS:-}" = "1" ]; then
  EXTRAS="[embeddings]"
fi
pip install --quiet -e ".${EXTRAS}"

if [ ! -f .env ]; then
  say "Creating .env from .env.example"
  cp .env.example .env
  echo "    Edit .env to add your Twilio / email credentials."
fi

say "Initialising database"
jobmon init-db

cat <<EOF

${BOLD}Setup complete.${RESET}

Next:
  1. Edit .env             — add your phone number and email credentials
  2. jobmon discover       — detect each company's careers platform
  3. jobmon doctor         — verify config, endpoints and channels
  4. jobmon test-notify    — send yourself a sample alert
  5. jobmon run            — start monitoring

Activate the venv in new shells with:  source .venv/bin/activate
EOF
