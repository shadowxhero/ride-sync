#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""骑行同步后的训练分析报告（v3，结构对齐 IntervalCoach；基于校准后的 FTP 基准）。

报告栏目：一句话总结 / 训练分析评分 / 元信息行 / 创纪录·历史最佳 / 体能影响 /
核心发现 / 观察 / 训练效果 / 付出 / 训练刺激 / 恢复影响 / 负荷 / 下一次训练 /
主区间行 / 对比(约5周前) / 恢复营养 / 建议。

用法:
  venv/bin/python3 ride_report.py --latest                    # 最近一条
  venv/bin/python3 ride_report.py --id i191195502             # 指定活动
  venv/bin/python3 ride_report.py --newly-synced 180 --send   # 同步流程自动推送
  venv/bin/python3 ride_report.py --latest --no-commentary    # 不用模型，模板兜底
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import urllib3
urllib3.disable_warnings()
import requests

BASE = Path(__file__).resolve().parent
TZ = timezone(timedelta(hours=8))
B = 'https://intervals.icu/api/v1'
STATE_FILE = BASE / '.ride_report.state.json'
CACHE_FILE = BASE / '.audit' / 'per_activity_curves.json'

DUR_LABELS = ((5, '5s'), (60, '1min'), (120, '2min'), (300, '5min'), (1200, '20min'), (3600, '1h'))
WD = '一二三四五六日'


def env_value(name, default=''):
    try:
        for line in (BASE / '.env').read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line.startswith(name + '='):
                return line.split('=', 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return default


APIK = env_value('INTERVALS_ICU_API_KEY')
S = requests.Session()
S.headers.update({'User-Agent': 'ride-sync/1.0', 'Accept': 'application/json'})
if APIK:
    S.auth = ('API_KEY', APIK)


def api_get(path, **params):
    r = S.get(B + path, params=params or None, timeout=90)
    r.raise_for_status()
    return r.json()


def num(v, scale=1.0):
    try:
        return float(v) / scale
    except (TypeError, ValueError):
        return None


def r0(v):
    return ('%.0f' % v) if v is not None else '-'


def r1(v):
    return ('%.1f' % v).rstrip('0').rstrip('.') if v is not None else '-'


def fmt_min(secs):
    return '%d min' % int((secs or 0) / 60.0 + 0.5)


def fmt_hms(secs):
    secs = int(secs or 0)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return '%d:%02d:%02d' % (h, m, s)


def parse_dt(s):
    s = str(s or '')[:19].replace('T', ' ')
    for f in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(s, f)
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------- 曲线缓存
def load_cache():
    try:
        if CACHE_FILE.exists():
            return json.loads(CACHE_FILE.read_text(encoding='utf-8'))
    except Exception:
        pass
    return {}


def save_cache(c):
    try:
        CACHE_FILE.parent.mkdir(exist_ok=True)
        CACHE_FILE.write_text(json.dumps(c, ensure_ascii=False), encoding='utf-8')
    except OSError:
        pass


def ensure_cache(acts, max_fetch=80):
    """保证有功率活动的逐条功率曲线都在缓存里（增量补齐）。"""
    cache = load_cache()
    todo = [a for a in acts if (a.get('icu_weighted_avg_watts') or a.get('icu_average_watts'))
            and a.get('id') not in cache]
    for a in todo[:max_fetch]:
        try:
            r = S.get(B + '/activity/%s/power-curve' % a['id'], timeout=60)
            if r.status_code == 200:
                c = r.json()
                secs = c.get('secs') or []
                vals = c.get('watts') or c.get('values') or []
                cache[a['id']] = {str(int(s)): float(w) for s, w in zip(secs, vals)}
        except Exception:
            pass
    if todo:
        save_cache(cache)
    return cache


# ---------------------------------------------------------------- 指标计算
def compute_score(act):
    """透明评分（0-10）：负荷35% + 时长20% + 心率漂移25% + 功率稳定性20%。

    对比 IntervalCoach：它给长时间骑行高分（146 TSS / 136min → 9/10）。
    旧算法只看「计划完成度」，无计划课时一律给 6 分，长骑会被低估。
    """
    tss = num(act.get('icu_training_load')) or 0
    minutes = (act.get('moving_time') or act.get('elapsed_time') or 0) / 60.0
    load = min(10.0, tss / 14.0)
    dur = min(10.0, minutes / 12.0)
    dec = num(act.get('decoupling'))
    if dec is None:
        drift = 7.0
    elif dec <= 0:
        drift = 10.0
    elif dec <= 3:
        drift = 8.0
    elif dec <= 5:
        drift = 8.0
    elif dec <= 8:
        drift = 6.0
    elif dec <= 12:
        drift = 4.0
    else:
        drift = 2.0
    vi = num(act.get('icu_variability_index'))
    if vi is None:
        stab = 7.0
    elif vi <= 1.08:
        stab = 10.0
    elif vi <= 1.15:
        stab = 8.0
    elif vi <= 1.25:
        stab = 6.0
    else:
        stab = 4.0
    score = 0.35 * load + 0.20 * dur + 0.25 * drift + 0.20 * stab
    comp = num(act.get('compliance'))
    paired = bool(act.get('paired_event_id'))
    effect = (min(comp, 110.0) / 11.0) if (paired and comp and comp > 0.5) else None
    return round(score, 1), (round(effect, 1) if effect is not None else None)


def score_trend(acts, target):
    """上周 vs 本周（含本次）的平均评分，0-100 分制。"""
    try:
        d0 = parse_dt(target.get('start_date_local'))
    except Exception:
        return None
    if not d0:
        return None
    week0 = d0.isocalendar()[1]
    year0 = d0.isocalendar()[0]
    buckets = {'prev': [], 'cur': []}
    for a in acts:
        d = parse_dt(a.get('start_date_local'))
        if not d or d > d0 + timedelta(days=1):
            continue
        if (d0 - d).days > 14:
            continue
        iso = d.isocalendar()
        key = 'cur' if (iso[0], iso[1]) == (year0, week0) else 'prev'
        sc, _ = compute_score(a)
        buckets[key].append(sc)
    if buckets['prev'] and buckets['cur']:
        prev = round(10 * sum(buckets['prev']) / len(buckets['prev']))
        cur = round(10 * sum(buckets['cur']) / len(buckets['cur']))
        return prev, cur
    return None


def pr_analysis(act, acts_by_id, cache):
    """返回 (破了哪些纪录, 与历史最佳的对比列表)。"""
    aid = act.get('id')
    cur = cache.get(aid) or {}
    d0 = parse_dt(act.get('start_date_local'))
    prs, comps = [], []
    for dur, label in DUR_LABELS:
        v = cur.get(str(dur))
        if v is None:
            continue
        best = bd = None
        for oid, c in cache.items():
            if oid == aid:
                continue
            x = acts_by_id.get(oid)
            if not x:
                continue
            od = parse_dt(x.get('start_date_local'))
            if not od or not d0 or od >= d0:
                continue
            vv = c.get(str(dur))
            if vv is None:
                continue
            if best is None or vv > best:
                best, bd = vv, od
        if best is None:
            continue
        if v > best + 0.5:
            prs.append({'label': label, 'cur': v, 'prev': best, 'date': bd.strftime('%Y-%m-%d'),
                        'dw': v - best, 'pct': 100.0 * (v - best) / best})
        else:
            comps.append({'label': label, 'cur': v, 'prev': best, 'date': bd.strftime('%Y-%m-%d'),
                          'pct': 100.0 * (v - best) / best})
    return prs, comps


def find_comparison(act, acts):
    """找 21-70 天前最相似的一次骑行（NP、距离接近），用于效率对比。"""
    d0 = parse_dt(act.get('start_date_local'))
    np0, km0 = num(act.get('icu_weighted_avg_watts')), num(act.get('distance'), 1000)
    if not d0 or not np0 or not km0:
        return None
    best = bs = None
    for x in acts:
        if x.get('id') == act.get('id'):
            continue
        d = parse_dt(x.get('start_date_local'))
        if not d:
            continue
        days = (d0 - d).days
        if not (21 <= days <= 70):
            continue
        npx, kmx = num(x.get('icu_weighted_avg_watts')), num(x.get('distance'), 1000)
        if not npx or not kmx:
            continue
        if abs(npx - np0) > 25 or abs(kmx - km0) / km0 > 0.35:
            continue
        s = abs(npx - np0) * 2 + abs(kmx - km0) / km0 * 100
        if bs is None or s < bs:
            bs, best = s, x
    if not best:
        return None
    d = parse_dt(best.get('start_date_local'))
    npx = num(best.get('icu_weighted_avg_watts'))
    res = {'id': best['id'], 'date': d.strftime('%Y-%m-%d'), 'weeks': int((d0 - d).days / 7),
           'dnp': np0 - npx, 'dnp_pct': 100.0 * (np0 - npx) / npx}
    h0, h1 = num(act.get('average_heartrate')), num(best.get('average_heartrate'))
    res['dhr'] = (h0 - h1) if (h0 and h1) else None
    return res


def stimulus_of(act):
    z = [x for x in (act.get('icu_zone_times') or []) if str(x.get('id') or '').startswith('Z')]
    if not z:
        return None
    secs = [x.get('secs') or 0 for x in z]
    total = float(sum(secs)) or 1.0
    low = sum(secs[:2]) / total
    mid = (secs[2] if len(secs) > 2 else 0) / total
    hi = sum(secs[3:]) / total
    # 口径对齐 IntervalCoach：Z3 为主 + Z4 若干 → 「节奏」（不是「阈值」）
    if hi >= 0.45:
        return '高强度'
    if hi >= 0.30:
        return '阈值'
    if mid >= 0.28:
        return '节奏'
    if low >= 0.70:
        return '耐力'
    return '节奏'


def zone_of(act):
    ftp = num(act.get('icu_ftp'))
    ap = num(act.get('icu_average_watts'))
    if not ftp or not ap:
        return None
    bounds = act.get('icu_power_zones') or [55, 75, 90, 105, 120, 150, 999]
    pct = 100.0 * ap / ftp
    for i, b in enumerate(bounds):
        try:
            if pct <= float(b):
                return 'Z%d' % (i + 1)
        except (TypeError, ValueError):
            continue
    return 'Z7'


def zone_pcts(act):
    z = [x for x in (act.get('icu_zone_times') or []) if str(x.get('id') or '').startswith('Z')]
    secs = [x.get('secs') or 0 for x in z]
    total = float(sum(secs)) or 0
    if not total:
        return ''
    parts = []
    for i, s in enumerate(secs[:5]):
        pct = 100.0 * s / total
        if pct >= 3:
            parts.append('Z%d %d%%' % (i + 1, round(pct)))
    tail = sum(secs[5:])
    if tail and 100.0 * tail / total >= 3:
        parts.append('Z6+ %d%%' % round(100.0 * tail / total))
    return ' · '.join(parts)


def zone_hi_share(act):
    """Z4 及以上时长占比（0-100）。"""
    z = [x for x in (act.get('icu_zone_times') or []) if str(x.get('id') or '').startswith('Z')]
    secs = [x.get('secs') or 0 for x in z]
    total = float(sum(secs)) or 0
    if not total:
        return None
    return 100.0 * sum(secs[3:]) / total


def fmt_recovery(hours):
    """恢复时长文本：>=36h 用「天」，否则用「小时」。"""
    h = hours or 0
    if h >= 36:
        d = h / 24.0
        return '约 %d 天' % round(d) if abs(d - round(d)) <= 0.25 else '约 %.1f 天' % d
    return '约 %d 小时' % round(h)


def load_tier(tss):
    """负荷分级。

    档位按 IntervalCoach 实测口径校准（它自己的两个样本）：
      54 TSS  → 轻微
      146 TSS → 中等
    旧档位 60/120/180 太严，导致常规骑行（~146 TSS）恒为「较大」、失去区分度。
    """
    t = tss or 0
    if t < 75:
        return '轻微'
    if t < 150:
        return '中等'
    if t < 250:
        return '较大'
    return '很大'


def nutrition_of(weight, moving_seconds=0):
    """恢复营养：碳水按骑行时长（~40g/小时），蛋白按体重（0.35 g/kg）。

    对齐 IntervalCoach 实测：123min / 69kg → 82g 碳水 + 24g 蛋白。
    （旧版写死 0.64/0.29 g/kg，与骑行时长无关，长骑会被低估。）
    """
    w = weight or 69.0
    hours = max(0.0, (moving_seconds or 0) / 3600.0)
    carbs = max(30, int(round(40.0 * hours / 2.0) * 2))
    prot = max(15, int(round(0.35 * w)))
    return carbs, prot


# ---------------------------------------------------------------- 模型（叙述部分）
def openclaw_candidates():
    cands = []
    w = shutil.which('openclaw')
    if w:
        cands.append(w)
    for c in ('/usr/local/bin/openclaw', '/root/.openclaw/tmp/agent-cli/openclaw'):
        if c not in cands and Path(c).exists():
            cands.append(c)
    if not cands:
        cands.append('openclaw')
    return cands


def _env():
    env = dict(os.environ)
    parts = [x for x in (env.get('PATH') or '').split(':') if x]
    for extra in ('/usr/local/bin', '/usr/bin', '/bin'):
        if extra not in parts:
            parts.append(extra)
    env['PATH'] = ':'.join(parts)
    return env


def model_text(prompt, limit=2000):
    for bin_path in openclaw_candidates():
        cmds = [
            [bin_path, 'capability', 'model', 'run', '--agent', 'main', '--json', '--prompt', prompt],
            [bin_path, 'capability', 'model', 'run', '--agent', 'main', '--local', '--json', '--prompt', prompt],
        ]
        model = os.environ.get('RIDE_REPORT_MODEL') or env_value('RIDE_REPORT_MODEL')
        if model:
            cmds = [c[:4] + ['--model', model] + c[4:] for c in cmds]
        for cmd in cmds:
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=240,
                                   cwd=str(BASE), env=_env())
            except (subprocess.TimeoutExpired, OSError) as e:
                print('[model] fail:', str(e)[:120])
                continue
            blob = (p.stdout or '') + '\n' + (p.stderr or '')
            for chunk in (p.stdout or '', blob):
                idx = chunk.find('{')
                while idx >= 0:
                    try:
                        j = json.loads(chunk[idx:])
                    except ValueError:
                        idx = chunk.find('{', idx + 1)
                        continue
                    for o in (j.get('outputs') or []):
                        t = str(o.get('text') or '').strip()
                        if t:
                            return t[:limit]
                    idx = -1
    return ''


def model_json(prompt):
    for attempt in range(2):
        txt = model_text(prompt + ('' if attempt == 0 else '\n只输出 JSON 对象本身，不要任何多余文字。'), 3000)
        if not txt:
            continue
        i, j = txt.find('{'), txt.rfind('}')
        if i >= 0 and j > i:
            try:
                return json.loads(txt[i:j + 1])
            except ValueError:
                pass
    return {}


# ---------------------------------------------------------------- 报告组装
def build_info(act, ctx):
    info = {}
    info['km'] = num(act.get('distance'), 1000)
    info['moving'] = act.get('moving_time') or act.get('elapsed_time') or 0
    info['elapsed'] = act.get('elapsed_time') or info['moving']
    info['gain'] = num(act.get('total_elevation_gain'))
    info['tss'] = num(act.get('icu_training_load'))
    info['np'] = num(act.get('icu_weighted_avg_watts'))
    info['ap'] = num(act.get('icu_average_watts'))
    info['hr'] = num(act.get('average_heartrate'))
    info['hrmax'] = num(act.get('max_heartrate'))
    info['ftp'] = num(act.get('icu_ftp'))
    info['weight'] = num(act.get('icu_weight')) or ctx.get('weight') or 69.0
    info['score'], info['effect'] = compute_score(act)
    info['stimulus'] = stimulus_of(act)
    info['zone'] = zone_of(act)
    info['load_tier'] = load_tier(info['tss'])
    _t = info['tss'] or 0
    # 恢复时长：短课 /4，长课 /3（对齐 IC 的两点实测：54→13h、146→~2天）
    info['rec_hours'] = max(4, min(96, round(_t / 4.0 if _t <= 60 else _t / 3.0)))
    info['drift'] = num(act.get('decoupling'))
    info['vi'] = num(act.get('icu_variability_index'))
    info['intensity'] = num(act.get('icu_intensity'))
    info['comp'] = num(act.get('compliance'))
    info['paired'] = bool(act.get('paired_event_id'))
    info['plan'] = ctx.get('plan') or {}
    info['next'] = ctx.get('next_workout') or {}
    info['prs'], info['comps'] = pr_analysis(act, ctx.get('acts_by_id') or {}, ctx.get('cache') or {})
    info['cmp'] = find_comparison(act, ctx.get('acts') or [])
    info['trend'] = score_trend(ctx.get('acts') or [], act)
    info['carbs'], info['prot'] = nutrition_of(info['weight'], info['moving'])
    wm = ctx.get('wellness') or {}
    d0 = parse_dt(act.get('start_date_local'))
    if d0:
        dprev = (d0 - timedelta(days=1)).strftime('%Y-%m-%d')
        dcur = d0.strftime('%Y-%m-%d')
        info['w_pre'] = wm.get(dprev) or {}
        info['w_cur'] = wm.get(dcur) or {}
    return info


def effort_label(info):
    """「付出」栏：有计划课就比完成度，没有就按负荷给整体判断。"""
    if info['paired'] and info['plan']:
        planned = num(info['plan'].get('icu_training_load'))
        if planned and info['tss']:
            ratio = info['tss'] / planned
            label = ('高于预期' if ratio >= 1.15 else
                     '低于预期' if ratio <= 0.85 else '符合预期')
            return '%s（实际 %s TSS / 计划 %s TSS）' % (label, r0(info['tss']), r0(planned))
    tss = info['tss'] or 0
    if tss >= 200:
        return '较高（%s TSS，长距离或高强度）' % r0(tss)
    if tss >= 120:
        return '充实（%s TSS）' % r0(tss)
    if tss >= 60:
        return '适中（%s TSS）' % r0(tss)
    return '轻松（%s TSS）' % r0(tss)


def narrative_prompt(act, info):
    L = []
    L.append('时间：%s' % parse_dt(act.get('start_date_local')).strftime('%Y-%m-%d（周%s）%H:%M')
             if parse_dt(act.get('start_date_local')) else '')
    L.append('名称：%s' % str(act.get('name') or '骑行'))
    L.append('距离/时长：%s km / %s 分钟（移动），爬升 %s m' %
             (r1(info['km']), round((info['moving'] or 0) / 60.0), r0(info['gain'])))
    if info['np']:
        L.append('功率：均 %sW · NP %sW（%s%% FTP）· 最大 %sW · VI %s' %
                 (r0(info['ap']), r0(info['np']), r0(info['intensity']), r0(max_w(act)), r1(info['vi'])))
    if info['hr']:
        L.append('心率：均 %s / 最大 %s · 漂移 %s%%' %
                 (r0(info['hr']), r0(info['hrmax']),
                  ('%+.1f' % info['drift']) if info['drift'] is not None else '-'))
    L.append('TSS：%s' % r0(info['tss']))
    zd = zone_pcts(act)
    if zd:
        L.append('功率区间分布：%s' % zd)
    hi = zone_hi_share(act)
    if hi is not None:
        L.append('高强度占比（Z4及以上）：%d%%' % int(hi + 0.5))
    if info['hr'] and info['hrmax']:
        share = ['最大心率 %d%%' % round(100.0 * info['hr'] / info['hrmax'])]
        lthr = num(act.get('lthr'))
        if lthr:
            share.append('阈值心率 %d%%' % round(100.0 * info['hr'] / lthr))
        L.append('心率占比：%s' % '、'.join(share))
    if info['paired'] and info['plan']:
        L.append('计划：%s（预计负荷 %s TSS）· 完成度 %s%%' %
                 (str(info['plan'].get('name'))[:50], r0(num(info['plan'].get('icu_training_load'))),
                  r0(info['comp'])))
    if info['stimulus']:
        L.append('训练刺激：%s · 评分 %.1f/10' % (info['stimulus'], info['score']))
    if info['prs']:
        for p in info['prs'][:2]:
            L.append('创纪录：%s %sW（上一次 %s %sW，%+.0fW）' %
                     (p['label'], r0(p['cur']), p['date'], r0(p['prev']), p['dw']))
    elif info['comps']:
        c = info['comps'][0]
        L.append('无新纪录；最近的历史对比：%s %sW（历史最佳 %sW，%+.1f%%）' %
                 (c['label'], r0(c['cur']), r0(c['prev']), c['pct']))
    if info['cmp']:
        seg = '对比 %s（%d周前）：NP %+.0fW' % (info['cmp']['date'], info['cmp']['weeks'], info['cmp']['dnp'])
        if info['cmp']['dhr'] is not None:
            seg += '、心率 %+.0fbpm' % info['cmp']['dhr']
        L.append(seg)
    L.append('恢复估计：%s；负荷等级：%s' % (fmt_recovery(info['rec_hours']), info['load_tier']))
    if info['next'].get('name'):
        L.append('下一次训练：%s %s' % (str(info['next'].get('start_date_local'))[:10],
                                       str(info['next'].get('name'))[:50]))
    return (
        '你是一名自行车教练。根据以下本次骑行数据，输出一个 JSON 对象（中文），字段：\n'
        '{"summary": "一句话总结，40字以内",\n'
        ' "findings": "核心发现，2-4句",\n'
        ' "observations": ["观察条目1", "观察条目2", "观察条目3"],\n'
        ' "advice": "给骑行者的建议，1-2句"}\n'
        '要求：只使用给定数据，数字必须准确，不要编造；所有百分比和数字必须直接引用上面给出的值，不要自行计算或改写；不要 Markdown；observations 2-4条，每条40字以内。\n'
        '【数据】\n' + '\n'.join('- ' + x for x in L if x)
    )


def max_w(act):
    return num(act.get('max_watts')) or num((act.get('icu_pm_p_max')))


def fallback_narrative(act, info):
    s = []
    if info['km'] is not None and info['moving']:
        s.append('完成了 %s km、约 %d 分钟的骑行' % (r1(info['km']), round((info['moving'] or 0) / 60.0)))
    if info['paired'] and info['comp']:
        s.append('计划完成度 %d%%' % round(info['comp']))
    summary = '，'.join(s) + '。' if s else '本次骑行已完成记录。'
    f = []
    if info['prs']:
        p = info['prs'][0]
        f.append('%s 功率 %sW 创下个人最佳（此前 %sW）。' % (p['label'], r0(p['cur']), r0(p['prev'])))
    elif info['comps']:
        c = info['comps'][0]
        f.append('本次 %s 为 %sW，低于历史最佳 %sW（%+.1f%%）。' % (c['label'], r0(c['cur']), r0(c['prev']), c['pct']))
    if info['cmp'] and info['cmp']['dhr'] is not None and info['cmp']['dhr'] <= -3:
        f.append('与 %d 周前相近强度相比，心率低 %d bpm，心血管效率提升。' %
                 (info['cmp']['weeks'], abs(round(info['cmp']['dhr']))))
    if info['tss']:
        f.append('训练负荷 %s TSS（%s）。' % (r0(info['tss']), info['load_tier']))
    obs = []
    if info['zone'] and info['elapsed']:
        obs.append('完成约 %d 分钟的骑行，整体处于 %s 强度。' % (round(info['elapsed'] / 60.0), info['zone']))
    if info['hr']:
        obs.append('平均心率 %s bpm，最大 %s bpm。' % (r0(info['hr']), r0(info['hrmax'])))
    if info['drift'] is not None:
        obs.append('心率漂移 %+.1f%%；功率分布：%s。' % (info['drift'], zone_pcts(act) or '—'))
    advice = '按计划进行下一次训练；注意恢复与补给。'
    return {'summary': summary, 'findings': ' '.join(f), 'observations': obs, 'advice': advice}


def build_report(act, ctx):
    info = build_info(act, ctx)
    nar = {}
    if not ctx.get('no_commentary'):
        nar = model_json(narrative_prompt(act, info)) or {}
    if not nar.get('summary'):
        nar = fallback_narrative(act, info) | {k: v for k, v in nar.items() if v}

    name = str(act.get('name') or '骑行')
    gid = (act.get('gear') or {}).get('id') or ''
    gname = (ctx.get('gear_names') or {}).get(gid) or ''
    dev = str(act.get('device_name') or '')
    extra = [x for x in (gname, dev) if x]
    dt = parse_dt(act.get('start_date_local'))

    L = []
    L.append('🚴 训练分析：%s' % name)
    if nar.get('summary'):
        L.append(str(nar['summary']).strip())

    L.append('')
    L.append('📊 训练分析 • %.1f/10' % info['score'])
    L.append(name + (('（%s）' % ' · '.join(extra)) if extra else ''))
    if dt:
        L.append('星期%s • %s • %s TSS • %s' %
                 (WD[dt.weekday()], fmt_min(info['moving']), r0(info['tss']), info['stimulus'] or '—'))

    if info['prs']:
        L.append('')
        L.append('🏆 创纪录')
        for p in info['prs'][:2]:
            L.append('%s · %sW' % (p['label'], r0(p['cur'])))
            L.append('上一次: %s %sW %+.0fW (%.1f%%)' % (p['date'], r0(p['prev']), p['dw'], p['pct']))
    elif info['comps']:
        L.append('')
        L.append('📚 历史最佳对比')
        for c in info['comps'][:4]:
            L.append('%s · %sW（历史最佳 %sW · %+.1f%%）' % (c['label'], r0(c['cur']), r0(c['prev']), c['pct']))

    if info['w_pre'] and info['w_cur']:
        c0, c1 = num(info['w_pre'].get('ctl')), num(info['w_cur'].get('ctl'))
        a0, a1 = num(info['w_pre'].get('atl')), num(info['w_cur'].get('atl'))
        if c0 is not None and c1 is not None:
            L.append('')
            L.append('📈 体能影响')
            L.append('CTL %s → %s（%+.1f）' % (r1(c0), r1(c1), c1 - c0))
            if a0 is not None and a1 is not None:
                L.append('TSB %+.1f → %+.1f（%+.1f）' % (c0 - a0, c1 - a1, (c1 - a1) - (c0 - a0)))
            if info['trend']:
                L.append('训练评分（周均） %d → %d（%+d）' % (info['trend'][0], info['trend'][1], info['trend'][1] - info['trend'][0]))

    if nar.get('findings'):
        L.append('')
        L.append('🔍 核心发现')
        L.append(str(nar['findings']).strip())

    if nar.get('observations'):
        L.append('')
        L.append('👀 观察')
        for o in (nar['observations'] or [])[:4]:
            L.append('· ' + str(o).strip())

    if info['effect'] is not None:
        L.append('')
        L.append('🎯 训练效果 %.1f/10' % info['effect'])
        if info['paired'] and info['plan']:
            L.append('计划「%s」完成度 %d%%，实际负荷 %s TSS（计划 %s TSS）。' %
                     (str(info['plan'].get('name'))[:40], round(info['comp'] or 0),
                      r0(info['tss']), r0(num(info['plan'].get('icu_training_load')))))

    eff = effort_label(info)
    if eff:
        L.append('')
        L.append('💦 付出：%s' % eff)

    if info['stimulus']:
        q = '充分' if info['score'] >= 8 else ('一般' if info['score'] >= 6 else '偏少')
        L.append('')
        L.append('⚡ 训练刺激：%s（%s）' % (info['stimulus'], q))

    L.append('')
    L.append('🛌 恢复影响：%s后可进行下一次高强度训练（按负荷估算）' % fmt_recovery(info['rec_hours']))
    L.append('🪫 负荷：%s' % info['load_tier'])

    if info['next'].get('name'):
        tsb_now = None
        try:
            w = ctx.get('latest_w') or {}
            if w.get('ctl') is not None and w.get('atl') is not None:
                tsb_now = num(w.get('ctl')) - num(w.get('atl'))
        except Exception:
            pass
        if tsb_now is not None and tsb_now < -25:
            adj = '建议先恢复，视状态减量'
        elif tsb_now is not None and tsb_now < -10:
            adj = '建议视状态调整强度'
        else:
            adj = '无需调整'
        nd = str(info['next'].get('start_date_local'))[:10]
        L.append('')
        L.append('🔜 下一次训练：%s %s（%s）· %s' %
                 (nd[5:], str(info['next'].get('name'))[:40], rel_day(nd), adj))

    if info['zone'] and info['elapsed']:
        L.append('')
        L.append('▶ 主区间：%dm @ %s · %s · %sW · %sbpm' %
                 (int(info['elapsed'] / 60.0 + 0.5), info['zone'], fmt_hms(info['elapsed']),
                  r0(info['ap']), r0(info['hr'])))

    if info['cmp']:
        L.append('')
        L.append('📅 对比 %s（%d周前）' % (info['cmp']['date'], info['cmp']['weeks']))
        L.append('功率 → %+.0fW（%+.1f%%）' % (info['cmp']['dnp'], info['cmp']['dnp_pct']))
        if info['cmp']['dhr'] is not None:
            trend = '提升中' if info['cmp']['dhr'] <= -3 else ('持平' if abs(info['cmp']['dhr']) <= 2 else '注意恢复')
            L.append('心率 → %+.0f bpm（%s）' % (info['cmp']['dhr'], trend))

    L.append('')
    L.append('🥤 恢复营养：%dg 碳水 + %dg 蛋白质（30-60 分钟内；如巧克力奶、酸奶+坚果、米饭+鸡胸）' %
             (info['carbs'], info['prot']))

    if nar.get('advice'):
        L.append('')
        L.append('💡 建议：' + str(nar['advice']).strip())

    L.append('')
    L.append('—— 阿爪 · 数据源 intervals.icu')
    return '\n'.join(L)


def rel_day(datestr):
    try:
        d = datetime.strptime(str(datestr)[:10], '%Y-%m-%d').date()
    except ValueError:
        return str(datestr)[:10]
    delta = (d - datetime.now(TZ).date()).days
    if delta == 0:
        return '今天'
    if delta == 1:
        return '明天'
    if delta == 2:
        return '后天'
    if delta > 0:
        return '%d天后' % delta
    return '%d天前' % (-delta)


# ---------------------------------------------------------------- 发送 / 状态
def _curl_send(tok, chat, text):
    curl = shutil.which('curl') or '/usr/bin/curl'
    try:
        p = subprocess.run(
            [curl, '-s', '-m', '60', '-X', 'POST',
             'https://api.telegram.org/bot%s/sendMessage' % tok,
             '--data-urlencode', 'chat_id=%s' % chat,
             '--data-urlencode', 'text=%s' % text],
            capture_output=True, text=True, timeout=90)
        try:
            j = json.loads(p.stdout or '{}')
        except ValueError:
            return False
        return bool(j.get('ok'))
    except Exception:
        return False


def send_telegram(text):
    tok = os.environ.get('RIDE_SYNC_BOT_TOKEN', '')
    chat = os.environ.get('RIDE_SYNC_CHAT_ID', '')
    if not tok or not chat:
        print('未设置 RIDE_SYNC_BOT_TOKEN / RIDE_SYNC_CHAT_ID，跳过发送')
        return False
    chunks = []
    while len(text) > 3900:
        cut = text.rfind('\n', 0, 3900)
        if cut < 1000:
            cut = 3900
        chunks.append(text[:cut])
        text = text[cut:].lstrip('\n')
    chunks.append(text)
    ok_all = True
    for i, part in enumerate(chunks):
        ok = False
        try:
            r = requests.post('https://api.telegram.org/bot%s/sendMessage' % tok,
                              data={'chat_id': chat, 'text': part, 'disable_web_page_preview': True},
                              timeout=30)
            if r.status_code < 400 and (r.json() or {}).get('ok'):
                ok = True
        except Exception as e:
            print('requests 发送异常（%s），尝试 curl 兜底' % str(e)[:100])
        if not ok:
            ok = _curl_send(tok, chat, part)
        ok_all = ok_all and ok
        if i + 1 < len(chunks):
            time.sleep(1)
    return ok_all


def load_state():
    try:
        if STATE_FILE.exists():
            return set(json.loads(STATE_FILE.read_text(encoding='utf-8')))
    except Exception:
        pass
    return set()


def save_state(ids):
    try:
        STATE_FILE.write_text(json.dumps(sorted(ids), ensure_ascii=False), encoding='utf-8')
    except OSError:
        pass


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--id')
    ap.add_argument('--latest', action='store_true')
    ap.add_argument('--newly-synced', type=int, default=0, metavar='MINUTES')
    ap.add_argument('--limit', type=int, default=3)
    ap.add_argument('--send', action='store_true')
    ap.add_argument('--no-commentary', action='store_true')
    args = ap.parse_args()

    if not APIK:
        print('缺少 INTERVALS_ICU_API_KEY（检查 .env）')
        return 1

    acts = api_get('/athlete/0/activities', oldest='2000-01-01T00:00:00',
                   newest='2027-01-01T00:00:00')
    acts_by_id = {a['id']: a for a in acts}

    gear_names = {}
    try:
        for g in api_get('/athlete/0/gear'):
            gear_names[g.get('id')] = g.get('name') or ''
    except Exception:
        pass

    latest_w = {}
    try:
        today = datetime.now(TZ).date()
        w = api_get('/athlete/0/wellness',
                    oldest=(today - timedelta(days=14)).isoformat(), newest=today.isoformat())
        if w:
            latest_w = w[-1] or {}
            ctx_wellness = {x.get('id'): x for x in w if x.get('id')}
        else:
            ctx_wellness = {}
    except Exception:
        ctx_wellness = {}

    cache = ensure_cache(acts)
    ctx_base = {
        'gear_names': gear_names, 'weight': num(latest_w.get('weight')) or 69.0,
        'wellness': ctx_wellness, 'latest_w': latest_w,
        'acts': acts, 'acts_by_id': acts_by_id, 'cache': cache,
        'no_commentary': args.no_commentary,
    }

    targets = []
    if args.id:
        aid = args.id.strip()
        if not aid.startswith('i'):
            aid = 'i' + aid
        targets = [api_get('/activity/' + aid)]
    elif args.newly_synced:
        now = datetime.now(timezone.utc)
        window = timedelta(minutes=args.newly_synced)
        done = load_state()
        newest = (datetime.now(TZ) + timedelta(days=1)).strftime('%Y-%m-%dT00:00:00')
        oldest = (datetime.now(TZ) - timedelta(days=14)).strftime('%Y-%m-%dT00:00:00')
        recent = api_get('/athlete/0/activities', oldest=oldest, newest=newest)
        for a in recent:
            sd = a.get('icu_sync_date')
            if not sd:
                continue
            try:
                t = datetime.fromisoformat(str(sd).replace('Z', '+00:00'))
            except ValueError:
                continue
            if now - t > window or a.get('id') in done:
                continue
            try:
                if float(a.get('distance') or 0) < 1000:
                    continue
            except (TypeError, ValueError):
                pass
            targets.append(a)
        targets.sort(key=lambda a: str(a.get('start_date_local')))
        targets = targets[-args.limit:]
    else:
        if acts:
            targets = [acts[0]]

    if not targets:
        print('没有需要分析的活动')
        return 0

    sent_ids = []
    for act in targets:
        aid = act.get('id')
        for _ in range(3):
            if act.get('icu_training_load') is not None or act.get('average_heartrate') or act.get('icu_weighted_avg_watts'):
                break
            time.sleep(5)
            try:
                act = api_get('/activity/' + aid)
            except Exception:
                break

        ctx = dict(ctx_base)
        if act.get('paired_event_id'):
            try:
                ctx['plan'] = api_get('/athlete/0/events/%s' % act['paired_event_id'])
            except Exception:
                pass
        try:
            d0 = parse_dt(act.get('start_date_local'))
            start = ((d0 - timedelta(days=1)).strftime('%Y-%m-%dT00:00:00') if d0 else
                     datetime.now(TZ).strftime('%Y-%m-%dT00:00:00'))
            end = (datetime.now(TZ) + timedelta(days=21)).strftime('%Y-%m-%dT00:00:00')
            evs = [e for e in api_get('/athlete/0/events', oldest=start, newest=end)
                   if e.get('category') == 'WORKOUT' and parse_dt(e.get('start_date_local'))]
            after = [e for e in evs if d0 and parse_dt(e.get('start_date_local')) > d0]
            now_dt = datetime.now(TZ).replace(tzinfo=None)
            if not after:
                after = [e for e in evs if parse_dt(e.get('start_date_local')) > now_dt]
            after.sort(key=lambda e: str(e.get('start_date_local')))
            if after and d0 and parse_dt(after[0].get('start_date_local')) < now_dt - timedelta(days=2):
                future = [e for e in evs if parse_dt(e.get('start_date_local')) > now_dt]
                future.sort(key=lambda e: str(e.get('start_date_local')))
                after = future or after
            if after:
                ctx['next_workout'] = after[0]
        except Exception:
            pass

        text = build_report(act, ctx)
        print(text)
        print('-' * 46)
        if args.send:
            ok = send_telegram(text)
            print('[发送]', aid, '成功' if ok else '失败')
            if ok:
                sent_ids.append(aid)
    if sent_ids:
        save_state(load_state() | set(sent_ids))
    return 0


if __name__ == '__main__':
    sys.exit(main())
