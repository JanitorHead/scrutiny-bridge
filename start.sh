#!/bin/sh
# scrutiny-bridge entrypoint. Standard library only — no dependencies to install.
set -e
exec python3 /app/app.py
