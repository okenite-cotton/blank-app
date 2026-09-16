#!/usr/bin/env bash
# Codespaces再接続時も、ダッシュボードを8501番で1つだけ起動します。
set -eu
cd /workspaces/blank-app
exec 9>/tmp/blank-app-dashboard-8501.lock
flock -n 9 || exit 0
if curl --fail --silent --max-time 3 http://127.0.0.1:8501/_stcore/health >/dev/null; then
  exit 0
fi
UV_BIN=$(command -v uv || printf '%s/.local/bin/uv' "$HOME")
export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/blank-app-template"
exec "$UV_BIN" run --project /workspaces/blank-app streamlit run streamlit_app.py --server.address 0.0.0.0 --server.port 8501 --server.enableCORS false --server.enableXsrfProtection false
