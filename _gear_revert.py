#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从备份恢复活动装备归属（gear_backup_*.json）。默认预检；--apply 执行。

用法: venv/bin/python3 _gear_revert.py gear_backup_20260929-005656.json [--apply]
"""
import json
import sys
import time
from pathlib import Path

import urllib3
urllib3.disable_warnings()
import requests

BASE = Path('/root/.openclaw/workspace/ride-sync')
Q = chr(34); SQ = chr(39)
KEY = ''
for line in (BASE / '.env').read_text(encoding='utf-8').splitlines():
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line:
        continue
    k, v = line.split('=', 1)
    if k.strip() == 'INTERVALS_ICU_API_KEY':
        KEY = v.strip().strip(Q).strip(SQ)
assert KEY, 'no key'

args = [a for a in sys.argv[1:] if not a.startswith('--')]
if not args:
    print('用法: _gear_revert.py <gear_backup.json> [--apply]')
    sys.exit(1)
backup = json.loads((BASE / args[0]).read_text(encoding='utf-8'))
apply_mode = '--apply' in sys.argv

S = requests.Session()
S.headers.update({'User-Agent': 'ride-sync/1.0', 'Accept': 'application/json'})
S.auth = ('API_KEY', KEY)
B = 'https://intervals.icu/api/v1'

todo = [b for b in backup if b.get('gear')]
print('备份 %d 条（含 gear %d 条）' % (len(backup), len(todo)))
if not apply_mode:
    for b in todo[:6]:
        print('  would restore', b['id'], str(b.get('date'))[:16], '->', b['gear'])
    print('（预检模式，加 --apply 执行）')
    sys.exit(0)

ok = 0; fail = 0
for i, b in enumerate(todo, 1):
    code = None
    for attempt in range(3):
        try:
            r = S.put(B + '/activity/' + str(b['id']), json={'gear': {'id': b['gear']}}, timeout=60)
            code = r.status_code
            if code == 200:
                break
        except Exception:
            code = 'EXC'
        time.sleep(1 + attempt)
    if code == 200:
        ok += 1
    else:
        fail += 1
        print('  FAIL', b['id'], code)
    if i % 40 == 0:
        print('  进度 %d/%d' % (i, len(todo)))
    time.sleep(0.15)
print('恢复完成: ok=%d fail=%d' % (ok, fail))
