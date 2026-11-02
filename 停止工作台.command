#!/bin/zsh
set -eu
cd -- "${0:A:h}"
export PATH="$HOME/.local/bin:$PATH"
backend/.venv/bin/python scripts/dev.py stop
