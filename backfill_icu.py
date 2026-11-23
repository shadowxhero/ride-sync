#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一次性脚本：把 iGPSPORT 的历史活动回填到 intervals.icu。

与 main.py 的日常同步相互独立：
  - 不读写同步游标（.igpsport.tmp），因此不会干扰正向同步；
  - 按「日期+里程」对 ICU 现有活动去重，可重复运行（幂等）；
  - 进度写入 .backfill_icu.state.json，中断后可续跑。

用法：
  venv/bin/python3 backfill_icu.py --dry-run --limit 5   # 先看会传哪些
  venv/bin/python3 backfill_icu.py --limit 3             # 小批量试
  venv/bin/python3 backfill_icu.py                       # 全量
  venv/bin/python3 backfill_icu.py --reset               # 清空进度重来
"""
import argparse
import json
import os
import time

import urllib3
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

STATE_FILE = '.backfill_icu.state.json'
RETRYABLE = ('429', '500', '502', '503', '504', '520', '521', '522', '524')


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get('done'), list):
                return data
        except (ValueError, OSError):
            pass
    return {'done': []}


def save_state(state):
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def classify(msg):
    """从异常文本判断是否值得重试。"""
    for code in RETRYABLE:
        if 'HTTP ' + code in msg:
            return 'retry'
    return 'fail'


def parse_args():
    p = argparse.ArgumentParser(description='回填 iGPSPORT 历史活动到 intervals.icu')
    p.add_argument('--limit', type=int, default=0, help='最多处理多少条（0=全部）')
    p.add_argument('--dry-run', action='store_true', help='只列出将要上传的活动')
    p.add_argument('--delay', type=float, default=1.0, help='每条之间的间隔秒数（默认 1.0）')
    p.add_argument('--reset', action='store_true', help='清空进度记录后重新开始')
    p.add_argument('--max-errors', type=int, default=5, help='连续失败多少条后中止')
    return p.parse_args()


def main():
    args = parse_args()
    load_dotenv()

    if args.reset and os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
        print('已清空进度记录', flush=True)

    state = load_state()
    done = {str(x) for x in state.get('done', [])}

    from igpsport import igpsport
    from intervalsicu import intervalsicu
    import main as m

    src = igpsport()
    src.login()
    all_acts = src.get_activity_list()
    print('iGPSPORT 活动总数: %d' % len(all_acts), flush=True)

    # 去重要比对 ICU 的完整历史，临时放宽回溯窗口（仅本进程有效）
    os.environ['INTERVALS_ICU_LOOKBACK_DAYS'] = '3650'
    dst = intervalsicu()
    dst.login()
    icu_list = dst.get_activity_list()
    print('intervals.icu 现有活动: %d' % len(icu_list), flush=True)

    tolerance = float(os.getenv('DEDUP_TOLERANCE_KM', '2.0'))

    todo = []
    for a in all_acts:
        if str(a['ride_id']) in done:
            continue
        if m.is_duplicate_activity(a, icu_list, tolerance):
            done.add(str(a['ride_id']))
            continue
        todo.append(a)

    if args.limit > 0:
        todo = todo[:args.limit]

    print('待上传 %d 条（已存在/已完成 %d 条）' % (len(todo), len(done)), flush=True)

    if args.dry_run:
        for a in todo[:50]:
            print('  DRY %s  %s' % (a['ride_id'], a['title']), flush=True)
        if len(todo) > 50:
            print('  ... 其余 %d 条省略' % (len(todo) - 50), flush=True)
        print('DRY-RUN 结束，未上传任何数据', flush=True)
        return

    uploaded = failed = 0
    consecutive = 0
    for i, a in enumerate(todo, 1):
        rid, title = str(a['ride_id']), a['title']
        try:
            fit = src.export_fit(a['origin_activity'])
        except Exception as exc:
            failed += 1
            consecutive += 1
            print('[%d/%d] 导出失败 %s %s: %s' % (i, len(todo), rid, title, exc), flush=True)
            if consecutive >= args.max_errors:
                print('连续失败过多，中止', flush=True)
                break
            continue

        ok = False
        for attempt in range(1, 6):
            try:
                dst.import_file(fit, title, '.fit')
                ok = True
                break
            except Exception as exc:
                msg = str(exc)
                if classify(msg) == 'retry' and attempt < 5:
                    wait = args.delay * (2 ** attempt)
                    print('  重试 %s 第%d次，等 %.0fs（%s）' % (rid, attempt, wait, msg[:70]), flush=True)
                    time.sleep(wait)
                    continue
                print('[%d/%d] 上传失败 %s %s: %s' % (i, len(todo), rid, title, msg[:120]), flush=True)
                break

        if ok:
            uploaded += 1
            consecutive = 0
            done.add(rid)
            state['done'] = sorted(done)
            save_state(state)
            icu_list.append(a)
            if i % 10 == 0 or i == len(todo):
                print('  进度 %d/%d  已上传=%d 失败=%d' % (i, len(todo), uploaded, failed), flush=True)
        else:
            failed += 1
            consecutive += 1
            if consecutive >= args.max_errors:
                print('连续失败过多，中止', flush=True)
                break
        time.sleep(args.delay)

    print('完成: 上传=%d 失败=%d 累计完成=%d' % (uploaded, failed, len(done)), flush=True)


if __name__ == '__main__':
    main()
