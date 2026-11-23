#!/usr/bin/env bash
# ride-sync 依赖自检：确保 venv 可用且依赖齐全，缺失则就地修复。
# 2026-11-19 事故：系统 Python 3.11 → 3.13，venv 符号链接失效，
# urllib3 等依赖全部不可用，每小时静默失败。
set -uo pipefail

WORKDIR="${1:-/root/.openclaw/workspace/ride-sync}"
cd "$WORKDIR" || exit 1

VENV="$WORKDIR/venv"
PY="$VENV/bin/python"
REQ="$WORKDIR/requirements.txt"
LOG="$WORKDIR/logs/venv-selfcheck.log"
mkdir -p "$WORKDIR/logs"

say() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"; }

check_modules() {
    "$PY" - <<'PY' 2>/dev/null
import importlib
mods = ["urllib3", "requests", "dotenv", "Crypto", "Crypto.Cipher.PKCS1_v1_5"]
missing = []
for m in mods:
    try:
        importlib.import_module(m)
    except Exception:
        missing.append(m)
print(" ".join(missing))
PY
}

venv_usable() {
    [ -x "$PY" ] || return 1
    local exe
    exe=$(sed -n 's/^executable *= *//p' "$VENV/pyvenv.cfg" 2>/dev/null | head -1)
    if [ -n "$exe" ] && [ ! -x "$exe" ]; then
        say "venv 失效：解释器不存在 ($exe)"
        return 1
    fi
    "$PY" -c "import sys" >/dev/null 2>&1 || { say "venv 失效：$PY 无法运行"; return 1; }
    return 0
}

rebuild_venv() {
    say "开始重建 ride-sync venv"
    local bak="$WORKDIR/venv.broken-$(date +%Y%m%d-%H%M%S)"
    [ -d "$VENV" ] && mv "$VENV" "$bak" && say "旧 venv 移至 $bak"
    python3 -m venv "$VENV" >/dev/null 2>&1 || { say "venv 创建失败"; return 1; }
    if ! "$PY" -m pip --version >/dev/null 2>&1; then
        say "缺 pip，尝试 get-pip"
        local gp=/tmp/get-pip-ride-$$.py
        curl -sS -m 120 -o "$gp" https://bootstrap.pypa.io/get-pip.py || { say "get-pip 下载失败"; return 1; }
        "$PY" "$gp" --quiet >>"$LOG" 2>&1 || { say "get-pip 失败"; rm -f "$gp"; return 1; }
        rm -f "$gp"
    fi
    return 0
}

install_requirements() {
    [ -f "$REQ" ] || { say "缺 requirements.txt"; return 1; }
    say "安装 requirements.txt"
    "$PY" -m pip install --quiet -r "$REQ" >>"$LOG" 2>&1
}

main() {
    if ! venv_usable; then
        rebuild_venv || { say "重建失败"; exit 1; }
        install_requirements || { say "安装失败"; exit 1; }
    else
        missing=$(check_modules)
        if [ -n "$missing" ]; then
            say "缺失依赖: $missing → 安装"
            install_requirements
            m2=$(check_modules)
            if [ -n "$m2" ]; then
                say "仍缺: $m2 → 重建"
                rebuild_venv || exit 1
                install_requirements || exit 1
                m3=$(check_modules)
                [ -n "$m3" ] && { say "仍缺失: $m3"; exit 1; }
            fi
        fi
    fi
    final=$(check_modules)
    [ -n "$final" ] && { say "自检失败: $final"; exit 1; }
    say "自检通过"
    exit 0
}

main
