#!/usr/bin/env sh
# KOL 红人邮件系统 - macOS / Linux 启动脚本
cd "$(dirname "$0")" || exit 1

PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >/dev/null 2>&1; then
      PY="$candidate"
      break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo "没有找到 Python 3.10 以上版本，请先安装：https://www.python.org/downloads/"
  exit 1
fi

exec "$PY" server/app.py
