#!/bin/zsh
set -eu
cd -- "${0:A:h}"
export PATH="$HOME/.local/bin:$PATH"
if backend/.venv/bin/python scripts/dev.py start; then
  open "http://127.0.0.1:3031"
else
  print '启动未完成。请把上方错误发给 Codex，勿粘贴密钥。'
  read '?按回车关闭。'
  exit 1
fi
