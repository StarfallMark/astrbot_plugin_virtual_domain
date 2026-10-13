#!/usr/bin/env sh
set -eu
SERVER_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PYTHONPATH="$SERVER_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUTF8=1

PYTHON=${VD_PYTHON:-python3}
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "找不到 Python 3。请安装 Python 3.11 或更新版本。" >&2
    exit 2
fi
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "虚拟域服务端需要 Python 3.11 或更新版本。" >&2
    exit 2
fi

MODEL_CONFIG=${VD_MODEL_CONFIG:-"$SERVER_ROOT/config/model.json"}
DB_PATH=${VD_DB_PATH:-"$SERVER_ROOT/data/virtual_domain.sqlite3"}
case "$DB_PATH" in
    /*) ;;
    *) DB_PATH="$SERVER_ROOT/$DB_PATH" ;;
esac
mkdir -p "$(dirname -- "$DB_PATH")"
PROFILE_STORE="$(dirname -- "$DB_PATH")/model-profiles.enc"
set -- --host "${VD_BIND_HOST:-127.0.0.1}" --port "${VD_PORT:-8787}" \
    --db "$DB_PATH" \
    --bot-mode "${VD_BOT_MODE:-standalone}" "$@"
if [ -n "${VD_TLS_CERT_FILE:-}" ] || [ -n "${VD_TLS_KEY_FILE:-}" ]; then
    if [ -z "${VD_TLS_CERT_FILE:-}" ] || [ -z "${VD_TLS_KEY_FILE:-}" ]; then
        echo "VD_TLS_CERT_FILE 和 VD_TLS_KEY_FILE 必须同时设置。" >&2
        exit 2
    fi
    set -- --tls-certfile "$VD_TLS_CERT_FILE" --tls-keyfile "$VD_TLS_KEY_FILE" "$@"
fi
if [ -n "${VD_TLS_CLIENT_CA_FILE:-}" ]; then
    if [ -z "${VD_TLS_CERT_FILE:-}" ]; then
        echo "双向 TLS 需要设置 VD_TLS_CERT_FILE 和 VD_TLS_KEY_FILE。" >&2
        exit 2
    fi
    set -- --tls-client-ca "$VD_TLS_CLIENT_CA_FILE" "$@"
fi
if [ -n "${VD_TLS_CLIENT_CRL_FILE:-}" ]; then
    if [ -z "${VD_TLS_CLIENT_CA_FILE:-}" ]; then
        echo "客户端证书吊销检查需要同时设置 VD_TLS_CLIENT_CA_FILE。" >&2
        exit 2
    fi
    set -- --tls-client-crl "$VD_TLS_CLIENT_CRL_FILE" "$@"
fi
if [ -f "$PROFILE_STORE" ]; then
    :
elif [ -f "$MODEL_CONFIG" ]; then
    set -- --model-config "$MODEL_CONFIG" "$@"
    if [ -n "${VD_MODEL_ID:-}" ]; then set -- --model-id "$VD_MODEL_ID" "$@"; fi
elif [ "${VD_ALLOW_NO_MODEL:-0}" = "1" ]; then
    echo "警告：以无模型事件模式启动，不能生成世界剧情或 Bot 回复。" >&2
else
    echo "缺少模型配置：既没有找到 $MODEL_CONFIG，也没有找到加密配置 $PROFILE_STORE。" >&2
    echo "请复制 config/model.example.json 为 config/model.json 并填写模型 API Key。" >&2
    echo "仅启动开发事件模式时设置 VD_ALLOW_NO_MODEL=1。" >&2
    exit 2
fi
if [ -n "${VD_CONNECTOR_CONFIG:-}" ]; then set -- --connector-config "$VD_CONNECTOR_CONFIG" "$@"; fi
if [ -n "${VD_CONNECTOR_TOKEN:-}" ]; then export VD_CONNECTOR_TOKEN; fi
if [ "${VD_BOT_MODE:-standalone}" = "astrbot" ] && [ -z "${VD_CONNECTOR_CONFIG:-}" ] && [ -z "${VD_CONNECTOR_TOKEN:-}" ]; then
    echo "AstrBot 模式需要 VD_CONNECTOR_CONFIG 或 VD_CONNECTOR_TOKEN。" >&2
    exit 2
fi
cd "$SERVER_ROOT"
echo "虚拟域服务端启动中：${VD_BIND_HOST:-127.0.0.1}:${VD_PORT:-8787}（Bot 模式：${VD_BOT_MODE:-standalone}）"
exec "$PYTHON" -m virtual_domain_server.server "$@"
