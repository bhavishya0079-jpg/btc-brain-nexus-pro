#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/../backend"
exec python3 -m app.main
