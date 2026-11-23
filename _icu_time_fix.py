#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 ICU 里来自 iGPSPORT 的活动时间对齐到 iGPSPORT 显示值。

用法:
  venv/bin/python3 _icu_time_fix.py               # 预检（只读）
  venv/bin/python3 _icu_time_fix.py --apply       # 执行（写备份）
  venv/bin/python3 _icu_time_fix.py --revert <backup.json>
"""
import datetime, json, sys
from pathlib import Path
import urllib3; urllib3.disable_warnings()
import requests

BASE=Path('/root/.openclaw/workspace/ride-sync')
Q=chr(34); SQ=chr(39)
KEY=''
for line in (BASE/'.env').read_text(encoding='utf-8').splitlines():
    line=line.strip()
    if not line or line.startswith('#') or '=' not in line: continue
    k,v=line.split('=',1)
    if k.strip()=='INTERVALS_ICU_API_KEY': KEY=v.strip().strip(Q).strip(SQ)
assert KEY, 'no key'

S=requests.Session()
S.headers.update({'User-Agent':'ride-sync/1.0','Accept':'application/json'})
S.auth=('API_KEY', KEY)
B='https://intervals.icu/api/v1'

FIX=[
 ('i190000024','2023-01-19T22:52:55','2023-01-19T14:52:55','55397022'),
 ('i190000320','2023-03-16T09:23:30','2023-03-16T01:23:30','55397023'),
 ('i190000809','2023-06-01T21:24:08','2023-06-01T13:24:08','55397024'),
 ('i190000826','2023-06-10T23:20:29','2023-06-10T15:20:29','55397025'),
 ('i190000841','2023-06-11T23:15:24','2023-06-11T15:15:24','55397026'),
 ('i190000849','2023-06-12T23:06:37','2023-06-12T15:06:37','55397027'),
 ('i190000957','2024-03-18T00:39:04','2024-03-17T16:39:04','55397028'),
 ('i189999343','2022-10-21T23:25:36','2022-10-21T15:25:36','55397294'),
 ('i190000729','2023-05-15T23:41:12','2023-05-15T15:41:12','55397300'),
 ('i190000868','2023-06-01T13:24:44','2023-06-18T04:04:12','3001392'),
]

def get_local(aid):
    j=S.get(B+'/activity/'+aid, timeout=60).json()
    return str(j.get('start_date_local') or '')[:19], str(j.get('name') or '')

def put_local(aid, val):
    r=S.put(B+'/activity/'+aid, json={'start_date_local': val}, timeout=60)
    return r.status_code

if '--revert' in sys.argv:
    p=Path(sys.argv[sys.argv.index('--revert')+1])
    bk=json.loads(p.read_text(encoding='utf-8'))
    print('revert items:', len(bk))
    for it in bk:
        code=put_local(it['icu_id'], it['old_local'])
        l2,_=get_local(it['icu_id'])
        print(' ', it['icu_id'], code, '->', l2)
    sys.exit(0)

apply_mode='--apply' in sys.argv

miss=[]
for icu_id, old, new, rid in FIX:
    cur, name=get_local(icu_id)
    mark='' if cur==old else '  <<< 与预期不符'
    if cur!=old: miss.append((icu_id, cur, old))
    print('%-12s %-19s -> %-19s | %s%s' % (icu_id, cur, new, name[:38], mark))

if miss:
    print('发现 %d 条与预期不符，停止。' % len(miss))
    sys.exit(1)

if not apply_mode:
    print('预检通过（%d 条）。加 --apply 执行。' % len(FIX))
    sys.exit(0)

stamp=datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
bkpath=BASE/('icu_time_backup_%s.json' % stamp)
backup=[]; allok=True
for icu_id, old, new, rid in FIX:
    code=put_local(icu_id, new)
    l2,_=get_local(icu_id)
    okk=(l2==new); allok=allok and okk
    backup.append({'icu_id':icu_id,'igp_ride':rid,'old_local':old,'new_local':new,'readback':l2})
    print(' ', icu_id, '->', l2, 'OK' if okk else 'FAIL')
bkpath.write_text(json.dumps(backup, ensure_ascii=False, indent=1), encoding='utf-8')
print('backup:', bkpath.name)

icu=S.get(B+'/athlete/0/activities', params={'oldest':'2000-01-01T00:00:00','newest':'2027-01-01T00:00:00'}, timeout=120).json()
det=json.load(open('/tmp/igp_details.json', encoding='utf-8'))
def parse(s):
    s=str(s)[:19].replace('T',' ')
    try: return datetime.datetime.strptime(s,'%Y-%m-%d %H:%M:%S')
    except Exception: return None
used=set(); bad=[]
for rid, d in det.items():
    if not isinstance(d, dict) or not d: continue
    km=(d.get('rideDistance') or 0)/1000.0
    t1=parse(d.get('startTime'))
    if not t1: continue
    best=None; bj=None; bt=None
    for j,a in enumerate(icu):
        if j in used: continue
        if abs((a.get('distance') or 0)/1000.0 - km)>0.12: continue
        t2=parse(a.get('start_date_local'))
        if not t2: continue
        dd=abs((t2-t1).total_seconds())
        if best is None or dd<best: best=dd; bj=j; bt=t2
    if bj is None: bad.append((rid,'NOMATCH')); continue
    used.add(bj)
    h=(bt-t1).total_seconds()/3600.0
    if abs(h)>0.05: bad.append((rid, round(h,2)))
print('verify mismatches:', len(bad))
for x in bad[:20]: print('  ', x)
print('DONE' if (allok and not bad) else 'CHECK NEEDED')
