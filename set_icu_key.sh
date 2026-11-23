#!/usr/bin/env bash
# 交互式写入 intervals.icu API key 到 ride-sync/.env
# 用法：cd /root/.openclaw/workspace/ride-sync && ./set_icu_key.sh
set -u

cd "$(dirname "$0")" || exit 1
ENV_FILE=.env

if [ ! -f "$ENV_FILE" ]; then
    echo "错误：找不到 $ENV_FILE"
    exit 1
fi

printf '粘贴 ICU API key 后回车（输入时不显示，正常现象）: '
read -r -s K
echo

# 去掉可能被粘进来的换行与首尾空白
K=$(printf '%s' "$K" | tr -d '\r\n' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')

if [ -z "$K" ]; then
    echo "错误：没有输入任何内容"
    exit 1
fi

case "$K" in
    '***'|xxx|XXX|your_api_key|changeme|test)
        echo "错误：这看起来是占位符，不是真实的 key"
        exit 1
        ;;
esac

if [ "${#K}" -lt 10 ]; then
    echo "错误：长度只有 ${#K} 位，不像真实的 ICU key（通常 20 位以上）"
    exit 1
fi

# 备份原文件（同样收紧权限，避免明文口令副本被放宽）
BK="$ENV_FILE.bak-$(date +%Y%m%d-%H%M%S)"
cp "$ENV_FILE" "$BK" && chmod 600 "$BK"

# 覆盖写入（printf 不解析 key 里的任何特殊字符）
grep -v '^INTERVALS_ICU_API_KEY=' "$ENV_FILE" > "$ENV_FILE.tmp"
printf 'INTERVALS_ICU_API_KEY=%s\n' "$K" >> "$ENV_FILE.tmp"
mv "$ENV_FILE.tmp" "$ENV_FILE"
chmod 600 "$ENV_FILE"

CHECK=$(grep -c '^INTERVALS_ICU_API_KEY=.\{10,\}' "$ENV_FILE" || true)
echo "输入长度=${#K}"
echo "文件校验=${CHECK}"
if [ "$CHECK" = "1" ]; then
    echo "写入成功"
    echo "备份：$BK"
else
    echo "写入校验失败，请把以上输出发给我"
    exit 1
fi
