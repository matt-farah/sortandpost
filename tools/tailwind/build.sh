#!/usr/bin/env sh
# Regenerate src/sortandpost/resources/tailwind.css from the classes used in
# src/sortandpost. Needs Node; commit the output (the app and Docker image never
# run Node). tests/test_tailwind_css.py checks the committed file is current.
set -eu
cd "$(dirname "$0")"
[ -d node_modules ] || npm ci --no-audit --no-fund
npm run --silent build
