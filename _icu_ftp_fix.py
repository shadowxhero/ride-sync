#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 intervals.icu 的骑行 FTP 统一改为 150。

- sport-settings(Ride) ftp=150（新活动使用）
- 全部活动的 icu_ftp=150（触发按新 FTP 重算强度/负荷/分区）

用法:
  venv/bin/python3 _icu_ftp_fix.py                 # 预览
  venv/bin/python3 _icu_ftp_fix.py --apply         # 执行（备份到 .audit/）
  venv/bin/python3 _icu_ftp_fix.py --revert <bk>   # 回退
"""
import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import urllib3
urllib3.disable_warnings()
import requests

BASE = Path(__file__).resolve().parent
Q = chr(34)
SQ = chr(39)
KEY = ''
for line in (BASE / '.env').read_text(encoding='utf-8').splitlines():
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line:
        continue
    k, v = line.split('=', 1)
    if k.strip() == 'INTERVALS_ICU_API_KEY':
        KEY = v.strip().strip(Q).strip(SQ)
assert KEY, 'no api key'

TARGET = 150
# 2026-09-29 探测阶段曾把该活动改成 150，这里记录真实初值以保证回退保真
KNOWN_PRIOR = {'i191195502': 250}
B = 'https://intervals.icu/api/v1'
S = requests.Session()
S.headers.update({'User-Agent': 'ride-sync/1.0', 'Accept': 'application/json'})
S.auth = ('API_KEY', KEY)


def get(p, **kw):
    r = S.get(B + p, params=kw or None, timeout=90)
    r.raise_for_status()
    return r.json()


def put(p, body):
    return S.put(B + p, json=body, timeout=90)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--revert')
    args = ap.parse_args()

    if args.revert:
        bk = json.loads(Path(args.revert).read_text(encoding='utf-8'))
        ss = bk['sport_settings']
        r = put('/athlete/0/sport-settings/%s' % ss['id'], {'ftp': ss['old']})
        print('sport-settings restore ->', ss['old'], r.status_code)
        if 'indoor_old' in ss:
            r = put('/athlete/0/sport-settings/%s' % ss['id'], {'indoor_ftp': ss['indoor_old']})
            print('indoor_ftp restore ->', ss['indoor_old'], r.status_code)
        ok = fail = 0
        for it in bk['activities']:
            if it.get('old') is None:
                continue
            done = False
            for _ in range(3):
                try:
                    if put('/activity/' + it['id'], {'icu_ftp': it['old']}).status_code == 200:
                        done = True
                        break
                except Exception:
                    pass
                time.sleep(1)
            ok += 1 if done else 0
            fail += 0 if done else 1
            time.sleep(0.1)
        print('revert done ok=%d fail=%d' % (ok, fail))
        return

    acts = get('/athlete/0/activities', oldest='2000-01-01T00:00:00',
               newest='2027-01-01T00:00:00')
    ssets = get('/athlete/0/sport-settings')
    ride = [e for e in ssets if 'Ride' in (e.get('types') or [])][0]
    need = [x for x in acts if x.get('icu_ftp') != TARGET]
    print('activities=%d | need update=%d | sport-settings ftp=%s' %
          (len(acts), len(need), ride.get('ftp')))

    sample_ids = ['i191195502', 'i190001938', 'i190001920']
    pre = {}
    for aid in sample_ids:
        a = get('/activity/' + aid)
        pre[aid] = (a.get('icu_intensity'), a.get('icu_training_load'))

    if not args.apply:
        print('dry-run. add --apply to execute.')
        print('pre(samples):', pre)
        return

    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    bk_path = BASE / '.audit' / ('icu_ftp_backup_%s.json' % stamp)
    bk_path.parent.mkdir(exist_ok=True)
    bk = {
        'sport_settings': {'id': ride['id'], 'old': ride.get('ftp')},
        'activities': [{'id': x['id'], 'old': KNOWN_PRIOR.get(x['id'], x.get('icu_ftp'))}
                       for x in acts],
    }
    bk_path.write_text(json.dumps(bk, ensure_ascii=False, indent=1), encoding='utf-8')
    print('backup:', bk_path.name)

    pilot = acts[-1]
    r = put('/activity/' + pilot['id'], {'icu_ftp': TARGET})
    print('pilot %s -> %s' % (pilot['id'], r.status_code))
    if r.status_code != 200:
        print('pilot failed, abort')
        sys.exit(1)

    r = put('/athlete/0/sport-settings/%s' % ride['id'], {'ftp': TARGET})
    print('sport-settings ->', r.status_code)

    ok = fail = 0
    fails = []
    for i, x in enumerate(acts, 1):
        done = False
        for _ in range(3):
            try:
                if put('/activity/' + x['id'], {'icu_ftp': TARGET}).status_code == 200:
                    done = True
                    break
            except Exception:
                pass
            time.sleep(1)
        if done:
            ok += 1
        else:
            fail += 1
            fails.append(x['id'])
        if i % 25 == 0:
            print('  progress %d/%d ok=%d fail=%d' % (i, len(acts), ok, fail))
        time.sleep(0.15)

    print('apply done ok=%d fail=%d' % (ok, fail))
    if fails:
        print('failed ids:', fails[:30])

    acts2 = get('/athlete/0/activities', oldest='2000-01-01T00:00:00',
                newest='2027-01-01T00:00:00')
    print('after icu_ftp dist:', dict(Counter(x.get('icu_ftp') for x in acts2)))
    for aid in sample_ids:
        a = get('/activity/' + aid)
        i0, t0 = pre.get(aid, (None, None))
        print('  %s  intensity %s -> %.1f | tl %s -> %s' %
              (aid, i0, a.get('icu_intensity') or 0, t0, a.get('icu_training_load')))


if __name__ == '__main__':
    main()
