#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

if [[ -x "$PROJECT_DIR/venv/bin/python" ]]; then
  PYTHON_BIN="$PROJECT_DIR/venv/bin/python"
elif command -v python3.10 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3.10)"
else
  echo "错误：未找到 Python 3.10。请创建 venv 或安装 Python 3.10 后重试。" >&2
  exit 1
fi

if ! "$PYTHON_BIN" -c 'import certifi' >/dev/null 2>&1; then
  echo "错误：缺少依赖。请运行：$PYTHON_BIN -m pip install -e ." >&2
  exit 1
fi

exec "$PYTHON_BIN" -m src "$@"
