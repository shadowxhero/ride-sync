#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 Gadgetbridge 导出的 SQLite 数据库转成 intervals.icu wellness 记录。

设计原则：**不预设表名/列名**。Gadgetbridge 的 schema 随版本变化，
且不同设备（Huami / Xiaomi protobuf）用不同表。所以本脚本先探测实际结构，
再按「列名特征」匹配到 ICU 的 wellness 字段。

用法：
  # 1) 先看数据库里有什么
  venv/bin/python3 gb_to_icu.py --db /path/to/Gadgetbridge --scan

  # 2) 看会生成哪些天、哪些值（不联网）
  venv/bin/python3 gb_to_icu.py --db /path/to/Gadgetbridge --dry-run

  # 3) 真正上传
  venv/bin/python3 gb_to_icu.py --db /path/to/Gadgetbridge --push

  # 只处理某个日期区间
  ... --from 2026-09-01 --to 2026-09-25
"""
import argparse
import base64
import json
import os
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))  # Asia/Shanghai

# ICU wellness 支持的数字字段（来自 OpenAPI 的 Wellness schema）
ICU_NUMERIC = {
    'restingHR', 'hrv', 'hrvSDNN', 'sleepSecs', 'sleepScore', 'sleepQuality',
    'avgSleepingHR', 'soreness', 'fatigue', 'stress', 'mood', 'motivation',
    'injury', 'spO2', 'systolic', 'diastolic', 'hydration', 'hydrationVolume',
    'readiness', 'baevskySI', 'bloodGlucose', 'lactate', 'bodyFat', 'abdomen',
    'vo2max', 'steps', 'respiration', 'carbohydrates', 'protein', 'fatTotal',
    'weight', 'kcalConsumed',
}

# 列名 → ICU 字段 的特征匹配（按顺序匹配，先匹配到先赢）
# 每条 = (ICU字段, 必须包含的关键词, 必须不包含的关键词)
COLUMN_RULES = [
    ('restingHR',   ('resting',),                  ('hrv',)),
    ('hrv',         ('hrv',),                      ('sdnn', 'summary', 'status')),
    ('hrvSDNN',     ('hrv', 'sdnn'),               ()),
    ('sleepScore',  ('sleep', 'score'),            ()),
    ('sleepSecs',   ('sleep', 'sec'),              ()),
    ('sleepSecs',   ('sleep', 'duration'),         ()),
    ('sleepSecs',   ('sleep', 'time'),             ('stage', 'start', 'end')),
    ('avgSleepingHR', ('sleep', 'heart'),          ()),
    ('spO2',        ('spo2',),                     ()),
    ('spO2',        ('blood', 'oxygen'),           ()),
    ('stress',      ('stress',),                   ()),
    ('respiration', ('respirat',),                 ()),
    ('respiration', ('breath',),                   ()),
    ('steps',       ('steps',),                    ()),
    ('weight',      ('weight',),                   ('temp',)),
    ('vo2max',      ('vo2',),                      ()),
    ('kcalConsumed', ('calorie',),                 ()),
    ('bodyFat',     ('body', 'fat'),               ()),
    ('readiness',   ('readiness',),                ()),
]

# 时间戳列候选
TS_COLS = ('timestamp', 'TIMESTAMP', 'time', 'TIME', 'date', 'DATE',
           'start_time', 'START_TIME', 'starttimestamp', 'STARTTIMESTAMP')

# 疑似"睡眠时段"的表（这些不是每日聚合值，要单独按天求和）
SLEEP_DURATION_HINTS = ('sleep', 'stage', 'session')


def is_numeric(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def to_local_date(ts):
    """Gadgetbridge 的时间戳是 unix 秒（有时是毫秒）。"""
    if ts is None:
        return None
    ts = float(ts)
    if ts > 1e12:      # 毫秒
        ts /= 1000.0
    if ts < 1e8:       # 明显不合理（可能是 HHMM 之类）
        return None
    return datetime.fromtimestamp(ts, TZ).date().isoformat()


# ---------------------------------------------------------------- schema 探测

def table_names(conn):
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'android_%' "
        "AND name NOT LIKE '%_Dao'")
    return [r[0] for r in cur.fetchall()]


def table_columns(conn, table):
    cur = conn.execute('PRAGMA table_info("%s")' % table)
    return [r[1] for r in cur.fetchall()]


def scan(conn, db_path):
    print('数据库: %s' % db_path)
    print('大小: %.1f MB' % (os.path.getsize(db_path) / 1e6))
    tables = table_names(conn)
    print('业务表数: %d\n' % len(tables))
    for t in sorted(tables):
        cols = table_columns(conn, t)
        try:
            n = conn.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
        except sqlite3.Error:
            n = -1
        if n == 0:
            continue
        ts_col = pick_ts_column(cols)
        span = ''
        if ts_col:
            try:
                lo, hi = conn.execute(
                    'SELECT MIN("%s"), MAX("%s") FROM "%s"' % (ts_col, ts_col, t)
                ).fetchone()
                d1, d2 = to_local_date(lo), to_local_date(hi)
                if d1 and d2:
                    span = '  %s ~ %s' % (d1, d2)
            except sqlite3.Error:
                pass
        print('  %-42s 行数=%-7s 列数=%-3d%s' % (t, n, len(cols), span))
        # 只对可能与 wellness 相关的表打印列
        if any(k in t.lower() for k in
               ('hrv', 'sleep', 'stress', 'spo2', 'hr', 'heart', 'steps',
                'weight', 'sample', 'summary', 'pai', 'energy', 'respir')):
            print('      列: %s' % ', '.join(cols))


def pick_ts_column(cols):
    low = {c.lower(): c for c in cols}
    for cand in TS_COLS:
        if cand.lower() in low:
            return low[cand.lower()]
    for c in cols:
        if 'timestamp' in c.lower() or c.lower() in ('time', 'date'):
            return c
    return None


# ---------------------------------------------------------------- 取值聚合

def match_icu_fields(cols, table=''):
    """把一组列名映射到 {ICU字段: 列名}。

    某些字段光看列名认不出，需要表名辅助：
      - 睡眠分期表里只有 DURATION/STAGE，要靠表名里含 sleep/stage 才敢认。
      - HRV 汇总表里的 weeklyAverage 等是统计值，不该当每日 hrv 上传。
    """
    lt = table.lower()
    sleep_table = any(k in lt for k in ('sleep', 'stage'))
    hrv_summary_table = 'summary' in lt
    out = {}
    for c in cols:
        lc = c.lower()
        # 睡眠表：DURATION/SECONDS/MINUTES 列 → 睡眠时长
        if 'sleepSecs' not in out and sleep_table:
            if 'duration' in lc or 'secs' in lc or 'sleep_time' in lc:
                out['sleepSecs'] = c
                continue
            if 'minute' in lc:
                out['sleepSecs'] = c
                continue
        # HRV 汇总表的统计列不当每日值
        if hrv_summary_table and any(k in lc for k in ('average', 'baseline', 'high')):
            continue
        for icu_field, must, mustnot in COLUMN_RULES:
            if icu_field in out:
                continue
            if all(m in lc for m in must) and not any(n in lc for n in mustnot):
                out[icu_field] = c
                break
    return out


def collect(conn, since, until):
    """返回 {date: {icu_field: value}}。"""
    by_day = defaultdict(lambda: defaultdict(list))
    tables = table_names(conn)
    used = []

    for t in tables:
        cols = table_columns(conn, t)
        ts_col = pick_ts_column(cols)
        if not ts_col:
            continue
        mapping = match_icu_fields(cols, t)
        if not mapping:
            continue

        sel = ', '.join('"%s"' % c for c in [ts_col] + list(mapping.values()))
        try:
            rows = conn.execute('SELECT %s FROM "%s"' % (sel, t)).fetchall()
        except sqlite3.Error:
            continue

        used.append((t, mapping, len(rows)))
        ncol = len(mapping) + 1
        for row in rows:
            day = to_local_date(row[0])
            if not day:
                continue
            if since and day < since:
                continue
            if until and day > until:
                continue
            for i, (icu_field, src_col) in enumerate(mapping.items(), start=1):
                v = row[i]
                if not is_numeric(v):
                    continue
                # 睡眠时长若以“分钟”名列（xxxMinute），换算成秒
                if icu_field == 'sleepSecs' and 'minute' in str(src_col).lower():
                    v = float(v) * 60.0
                by_day[day][icu_field].append(float(v))
    return by_day, used


def aggregate(by_day):
    """把一天内的多个采样聚合为单值（ICU wellness 每天只有一个值）。"""
    out = {}
    for day, fields in by_day.items():
        rec = {}
        for f, vals in fields.items():
            if not vals:
                continue
            if f == 'steps':
                rec[f] = int(round(sum(vals)))
            elif f == 'sleepSecs':
                rec[f] = int(round(sum(vals)))
            elif f == 'spO2':
                rec[f] = round(min(vals), 1)        # 血氧取最低，更保守
            elif f == 'restingHR':
                rec[f] = int(round(statistics.median(vals)))
            elif f in ('hrv', 'hrvSDNN', 'respiration'):
                rec[f] = round(statistics.median(vals), 2)
            elif f in ('stress', 'sleepScore', 'readiness', 'vo2max',
                       'bodyFat', 'weight', 'avgSleepingHR'):
                rec[f] = round(vals[-1], 2)          # 取当天最后一个
            else:
                rec[f] = round(statistics.median(vals), 2)
        if rec:
            out[day] = rec
    return out


# ---------------------------------------------------------------- ICU 上传

def icu_auth():
    key = (os.getenv('INTERVALS_ICU_API_KEY') or '').strip()
    if not key:
        from dotenv import load_dotenv
        load_dotenv()
        key = (os.getenv('INTERVALS_ICU_API_KEY') or '').strip()
    if not key:
        raise SystemExit('缺少 INTERVALS_ICU_API_KEY')
    return 'Basic ' + base64.b64encode(('API_KEY:' + key).encode()).decode()


def icu_put(auth, athlete, day, payload, tries=4):
    data = json.dumps(payload).encode()
    url = 'https://intervals.icu/api/v1/athlete/%s/wellness/%s' % (athlete, day)
    for attempt in range(1, tries + 1):
        req = urllib.request.Request(url, data=data, method='PUT')
        for k, v in (('Authorization', auth), ('Accept', 'application/json'),
                     ('Content-Type', 'application/json'),
                     ('User-Agent', 'ride-sync/1.0')):
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < tries:
                time.sleep(2 ** attempt)
                continue
            return e.code
        except Exception:
            if attempt < tries:
                time.sleep(2 ** attempt)
                continue
            return None
    return None


def parse_args():
    p = argparse.ArgumentParser(description='Gadgetbridge SQLite → intervals.icu wellness')
    p.add_argument('--db', required=True, help='Gadgetbridge 导出的数据库文件路径')
    p.add_argument('--scan', action='store_true', help='只打印数据库结构')
    p.add_argument('--dry-run', action='store_true', help='只展示将上传的内容')
    p.add_argument('--push', action='store_true', help='真正上传到 ICU')
    p.add_argument('--from', dest='since', help='起始日期 YYYY-MM-DD')
    p.add_argument('--to', dest='until', help='结束日期 YYYY-MM-DD')
    p.add_argument('--athlete', default='0', help='athlete id（默认 0）')
    p.add_argument('--only-fields', help='逗号分隔，只上传这些字段')
    return p.parse_args()


def main():
    args = parse_args()
    if not os.path.exists(args.db):
        raise SystemExit('找不到数据库文件: %s' % args.db)

    conn = sqlite3.connect(args.db)
    try:
        if args.scan:
            scan(conn, args.db)
            return

        by_day, used = collect(conn, args.since, args.until)
        if not used:
            print('没有匹配到任何 wellness 相关表。请先运行 --scan 查看结构。')
            return

        print('匹配到的表:')
        for t, mapping, n in used:
            print('  %-40s %s  (%d 行)' % (t, mapping, n))

        records = aggregate(by_day)
        if not records:
            print('\n没有可上传的记录。')
            return

        if args.only_fields:
            keep = {f.strip() for f in args.only_fields.split(',') if f.strip()}
            records = {d: {k: v for k, v in r.items() if k in keep}
                       for d, r in records.items()}
            records = {d: r for d, r in records.items() if r}

        days = sorted(records)
        print('\n将处理 %d 天：%s ~ %s' % (len(days), days[0], days[-1]))
        field_usage = defaultdict(int)
        for r in records.values():
            for k in r:
                field_usage[k] += 1
        print('字段覆盖：')
        for f, n in sorted(field_usage.items(), key=lambda x: -x[1]):
            print('  %-16s %d/%d 天' % (f, n, len(days)))

        for d in days[:5]:
            print('  %s  %s' % (d, records[d]))
        if len(days) > 5:
            print('  ...（其余 %d 天）' % (len(days) - 5))

        if args.dry_run or not args.push:
            print('\n[dry-run] 未上传。加 --push 才会真正写入。')
            return

        auth = icu_auth()
        ok = fail = 0
        for i, d in enumerate(days, 1):
            st = icu_put(auth, args.athlete, d, records[d])
            if st in (200, 201, 204):
                ok += 1
            else:
                fail += 1
                print('  失败 %s -> HTTP %s' % (d, st))
            if i % 20 == 0 or i == len(days):
                print('  进度 %d/%d  成功=%d 失败=%d' % (i, len(days), ok, fail))
            time.sleep(1.0)
        print('\n完成：成功 %d，失败 %d' % (ok, fail))
    finally:
        conn.close()


if __name__ == '__main__':
    main()
