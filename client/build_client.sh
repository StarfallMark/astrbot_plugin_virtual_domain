#!/usr/bin/env sh
set -eu

CLIENT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OS_NAME=$(uname -s)
MACHINE=$(uname -m)

case "$OS_NAME" in
    Darwin) PLATFORM=macos ;;
    Linux) PLATFORM=linux ;;
    *)
        echo "此脚本用于 Linux 和 macOS；Windows 请运行 build_client.ps1。" >&2
        exit 2
        ;;
esac

case "$MACHINE" in
    x86_64|amd64) ARCH=x86_64 ;;
    arm64|aarch64) ARCH=arm64 ;;
    *)
        echo "未识别的目标架构：$MACHINE" >&2
        exit 2
        ;;
esac

MODE=onedir
PYTHON=${VD_PYTHON:-}
OUTPUT_ROOT=${VD_CLIENT_OUTPUT:-"$CLIENT_ROOT/dist/$PLATFORM-$ARCH"}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --onefile)
            MODE=onefile
            shift
            ;;
        --onedir)
            MODE=onedir
            shift
            ;;
        --python)
            if [ "$#" -lt 2 ]; then
                echo "--python 后需要指定解释器路径。" >&2
                exit 2
            fi
            PYTHON=$2
            shift 2
            ;;
        --output)
            if [ "$#" -lt 2 ]; then
                echo "--output 后需要指定输出目录。" >&2
                exit 2
            fi
            OUTPUT_ROOT=$2
            shift 2
            ;;
        --help|-h)
            cat <<'EOF'
用法：sh build_client.sh [--onedir|--onefile] [--python PYTHON] [--output DIR]

默认生成目录版：macOS 输出 VirtualDomain.app，Linux 输出 VirtualDomain/。
--onefile 可生成单文件程序；macOS 仍会生成 .app，但不建议用单文件模式分发。
可以用 VD_PYTHON 和 VD_CLIENT_OUTPUT 设置默认解释器及输出目录。
EOF
            exit 0
            ;;
        *)
            echo "未知参数：$1（运行 --help 查看用法）" >&2
            exit 2
            ;;
    esac
done

if [ -z "$PYTHON" ]; then
    if [ -x "$CLIENT_ROOT/.venv/bin/python" ]; then
        PYTHON=$CLIENT_ROOT/.venv/bin/python
    elif command -v python3 >/dev/null 2>&1; then
        PYTHON=$(command -v python3)
    elif command -v python >/dev/null 2>&1; then
        PYTHON=$(command -v python)
    else
        echo "没有找到 Python。请安装 Python 3.11 或更新版本。" >&2
        exit 2
    fi
fi

case "$PYTHON" in
    */*) ;;
    *)
        if ! command -v "$PYTHON" >/dev/null 2>&1; then
            echo "找不到 Python：$PYTHON" >&2
            exit 2
        fi
        PYTHON=$(command -v "$PYTHON")
        ;;
esac

if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "虚拟域客户端打包需要 Python 3.11 或更新版本。" >&2
    exit 2
fi
if ! "$PYTHON" -c 'import PyInstaller, PySide6' >/dev/null 2>&1; then
    echo '打包依赖尚未安装。请在 client 目录执行：python3 -m pip install -e ".[packaging]"' >&2
    exit 2
fi

case "$OUTPUT_ROOT" in
    /*) ;;
    *) OUTPUT_ROOT=$CLIENT_ROOT/$OUTPUT_ROOT ;;
esac
BUILD_ROOT=$CLIENT_ROOT/build/release/$PLATFORM-$ARCH
mkdir -p "$OUTPUT_ROOT" "$BUILD_ROOT"
export PYINSTALLER_CONFIG_DIR=$BUILD_ROOT/pyinstaller-config

set -- -m PyInstaller --noconfirm --clean "--$MODE" \
    --name VirtualDomain \
    --workpath "$BUILD_ROOT/work" \
    --specpath "$BUILD_ROOT/spec" \
    --distpath "$OUTPUT_ROOT" \
    --paths "$CLIENT_ROOT/src"
if [ "$PLATFORM" = macos ]; then
    set -- "$@" --windowed
fi
set -- "$@" "$CLIENT_ROOT/src/virtual_domain_client/app.py"

cd "$CLIENT_ROOT"
"$PYTHON" "$@"

if [ "$PLATFORM" = macos ]; then
    if [ ! -d "$OUTPUT_ROOT/VirtualDomain.app" ]; then
        echo "打包结束，但没有生成 $OUTPUT_ROOT/VirtualDomain.app" >&2
        exit 1
    fi
    echo "macOS 客户端已生成：$OUTPUT_ROOT/VirtualDomain.app"
elif [ "$MODE" = onedir ]; then
    if [ ! -x "$OUTPUT_ROOT/VirtualDomain/VirtualDomain" ]; then
        echo "打包结束，但没有生成 $OUTPUT_ROOT/VirtualDomain/VirtualDomain" >&2
        exit 1
    fi
    echo "Linux 客户端已生成：$OUTPUT_ROOT/VirtualDomain/VirtualDomain"
else
    if [ ! -x "$OUTPUT_ROOT/VirtualDomain" ]; then
        echo "打包结束，但没有生成 $OUTPUT_ROOT/VirtualDomain" >&2
        exit 1
    fi
    echo "Linux 客户端已生成：$OUTPUT_ROOT/VirtualDomain"
fi
echo "目标平台：$PLATFORM-$ARCH；构建模式：$MODE"
