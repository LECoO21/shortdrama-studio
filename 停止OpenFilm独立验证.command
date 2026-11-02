#!/bin/zsh
set -eu
cd "${0:A:h}"
python3 scripts/openfilm_poc.py stop
