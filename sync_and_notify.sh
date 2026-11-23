#!/usr/bin/env bash
# ride-sync 数据同步 + Telegram 通知
# 不经过模型层，直接执行脚本并用 Telegram Bot API 发通知
set -uo pipefail

cd /root/.openclaw/workspace/ride-sync

# 落盘一份运行日志：以后判断「网络问题 vs 脚本问题」有据可查
# （提前定义，自检失败与通知发送失败也要能记进同一份日志）
LOG_DIR="$(dirname "$0")/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/sync-$(date +%Y-%m-%d).log"

log_line() {
    echo "===== $(date '+%Y-%m-%d %H:%M:%S %z') $* =====" >> "$LOG_FILE" 2>/dev/null
}

# 通知凭据从 .env 读取（2026-11-24 从脚本硬编码移出，并提前到自检前定义）
# 容错：export 前缀、等号两侧空白、单/双引号、行尾注释、CRLF
env_get() {
    local key="$1" line
    [ -f .env ] || return 0
    line="$(sed -n "s/^[[:space:]]*\(export[[:space:]]\+\)\?${key}[[:space:]]*=//p" .env | tail -1)"
    [ -n "$line" ] || return 0
    printf '%s' "$line" \
        | sed -e 's/[[:space:]]#.*$//' \
              -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
              -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/" \
        | tr -d '\r'
}
BOT_TOKEN="$(env_get BOT_TOKEN)"
CHAT_ID="$(env_get CHAT_ID)"

if [ -z "$BOT_TOKEN" ] || [ -z "$CHAT_ID" ]; then
    echo "ride-sync: 未能从 .env 解析到 BOT_TOKEN/CHAT_ID，本轮 Telegram 通知将失效" >&2
    log_line "WARN missing BOT_TOKEN/CHAT_ID"
fi

# 统一发送入口：凭据缺失/HTTP 非 200 都不再静默
# （原来 >/dev/null 2>&1 会把 /bot/sendMessage 的 404 完全吞掉）
tg_send() {
    local text="$1" out code
    if [ -z "$BOT_TOKEN" ] || [ -z "$CHAT_ID" ]; then
        log_line "WARN telegram skipped: missing BOT_TOKEN/CHAT_ID"
        return 1
    fi
    out="$(curl -sS -m 30 -w '\n%{http_code}' -X POST \
        "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${CHAT_ID}" \
        --data-urlencode "text=${text}" 2>&1)"
    code="${out##*$'\n'}"
    if [ "$code" != "200" ]; then
        log_line "WARN telegram send failed http=${code} body=${out%$'\n'*}"
        return 1
    fi
    return 0
}

# ── 依赖自检（2026-11-19 新增）──
# 系统升级 Python 会让 venv 失效；此前会每小时静默失败（ModuleNotFoundError: urllib3）。
# 这里先自检，缺失则自动重建 venv + 装依赖。
if [ -x "$PWD/ensure_venv.sh" ]; then
    if ! bash "$PWD/ensure_venv.sh" "$PWD"; then
        MSG="⚠️ ride-sync 环境自检失败

Python 依赖无法自动修复，本轮同步未执行。
详情：/root/.openclaw/workspace/ride-sync/logs/venv-selfcheck.log"
        tg_send "$MSG" || true
        log_line "exit=1 (venv selfcheck)"
        exit 1
    fi
fi


# 从 .env 读实际目标平台，避免文案与实际同步方向不符。
# IMPORT_PLATFORM 支持逗号分隔的多个目标，这里逐个映射成可读名称。
platform_label() {
    case "$1" in
        intervalsicu) printf 'intervals.icu' ;;
        imxingzhe)    printf '行者' ;;
        igpsport)     printf 'iGPSPORT' ;;
        '')           printf '' ;;
        *)            printf '%s' "$1" ;;
    esac
}

IMPORT_PLATFORM="$(env_get IMPORT_PLATFORM)"
TARGET_LABEL=""
_old_ifs="$IFS"; IFS=','
for _p in $IMPORT_PLATFORM; do
    # 去掉首尾空白
    _p="$(printf '%s' "$_p" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    [ -z "$_p" ] && continue
    _lbl="$(platform_label "$_p")"
    if [ -z "$TARGET_LABEL" ]; then
        TARGET_LABEL="$_lbl"
    else
        TARGET_LABEL="${TARGET_LABEL}、${_lbl}"
    fi
done
IFS="$_old_ifs"
[ -z "$TARGET_LABEL" ] && TARGET_LABEL="未知平台"

run_sync() {
    OUTPUT="$(venv/bin/python3 main.py 2>&1)"
    EXIT_CODE=$?
    SYNC_LINE="$(echo "$OUTPUT" | grep '^SYNC_RESULT:' | tail -1)"
    {
        echo "===== $(date '+%Y-%m-%d %H:%M:%S %z') exit=${EXIT_CODE}${1:-} ====="
        echo "$OUTPUT"
        if [ -n "$SYNC_LINE" ]; then echo "PARSED: $SYNC_LINE"; fi
    } >> "$LOG_FILE" 2>&1
}

run_sync
# main.py 内已对瞬时网络故障重试；若整轮仍失败，隔 60s 再试一次，
# 覆盖更长的服务端抖动，避免误告警（两次都失败才上报）。
if [ $EXIT_CODE -ne 0 ]; then
    sleep 60
    run_sync " (第2次尝试)"
fi

# 同步报错时发错误通知
if [ $EXIT_CODE -ne 0 ]; then
    MSG="⚠️ iGPSport→${TARGET_LABEL} 同步失败（exit=$EXIT_CODE）
${OUTPUT: -500}"
    tg_send "$MSG" || true
    exit $EXIT_CODE
fi

# 没有 SYNC_RESULT 行（异常但没报错）→ 静默退出
if [ -z "$SYNC_LINE" ]; then
    exit 0
fi

# 解析 synced 数量
SYNCED="$(echo "$SYNC_LINE" | sed -n 's/.*synced=\([0-9]*\).*/\1/p')"

# 有新活动 → 发通知
if [ "${SYNCED}" -gt 0 ] 2>/dev/null; then
    TITLES="$(echo "$SYNC_LINE" | sed -n "s/.*titles=\[\(.*\)\]/\1/p")"
    MSG="【ride-sync同步】 🚴 已同步 $SYNCED 条新活动到${TARGET_LABEL}：
$TITLES"
    tg_send "$MSG" || true

    # 同一条新活动：附上自动骑行分析（数据已进 ICU，可立即读取）
    REPORT_PY="$(dirname "$0")/ride_report.py"
    if [ -f "$REPORT_PY" ]; then
        RIDE_REPORT_MODEL="$(env_get RIDE_REPORT_MODEL)"
        _rargs=(--newly-synced 180 --send)
        [ -n "$RIDE_REPORT_MODEL" ] && _rargs+=(--commentary-model "$RIDE_REPORT_MODEL")
        RIDE_SYNC_BOT_TOKEN="$BOT_TOKEN" RIDE_SYNC_CHAT_ID="$CHAT_ID" \
            venv/bin/python3 "$REPORT_PY" "${_rargs[@]}" \
            >> "$LOG_DIR/ride-report-$(date +%Y-%m-%d).log" 2>&1 || true
    fi
fi

exit 0