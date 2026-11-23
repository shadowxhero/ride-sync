#!/usr/bin/env python
# -*- coding: utf-8 -*一
import os
import time
import re
import json
import socket
from datetime import datetime, timedelta

import urllib3
import requests
from dotenv import load_dotenv
from igpsport import igpsport
from imxingzhe import imxingzhe
from intervalsicu import intervalsicu

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 只有「瞬时网络故障」才重试；凭据错误、配置错误等一律直接抛出。
# 注：requests 会把 urllib3 的 MaxRetryError / RemoteDisconnected 包成 ConnectionError。
TRANSIENT_ERRORS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
    socket.timeout,
)

# 登录/同步遇对端瞬时掐断（RemoteDisconnected 等）时重试，避免整轮失败发告警。
# 同步游标是「推进式」的，重跑天然幂等（已成功的目标靠去重跳过），所以重试安全。
LOGIN_ATTEMPTS = int(os.getenv('LOGIN_ATTEMPTS', '3') or 3)
LOGIN_BACKOFF = float(os.getenv('LOGIN_BACKOFF', '3') or 3)
SYNC_ATTEMPTS = int(os.getenv('SYNC_ATTEMPTS', '3') or 3)
SYNC_BACKOFF = float(os.getenv('SYNC_BACKOFF', '5') or 5)
# 重试总时长预算（整个进程共享）。
# 定时任务自身超时 300s，历史最慢单次已 135s —— 预算必须留足余量，
# 否则「重试把任务拖超时」会被当成失败上报，反而制造新告警。
# 实测瞬时断连（RemoteDisconnected）会很快失败，几秒内就能重试完，60s 足够。
RETRY_BUDGET = float(os.getenv('RETRY_BUDGET_SECONDS', '60') or 60)
_RUN_STARTED = time.time()


def _remaining_budget() -> float:
    return RETRY_BUDGET - (time.time() - _RUN_STARTED)

# 各平台把底层异常包成了普通 Exception，这里用文本特征识别「可重试的瞬时故障」。
_RETRYABLE_MARKERS = (
    'connection aborted', 'remote disconnected', 'max retries exceeded',
    'connection reset', 'connection refused', 'timed out', 'timeout',
    'temporarily unavailable', 'temporary failure', 'ssl', 'eof occurred',
    'incompleteread', 'bad gateway', 'service unavailable',
    'http 429', 'http 500', 'http 502', 'http 503', 'http 504',
)


def is_transient(exc) -> bool:
    """是否属于「重试就可能成功」的瞬时故障（凭据/配置错误不算）。"""
    if isinstance(exc, TRANSIENT_ERRORS):
        return True
    msg = str(exc).lower()
    return any(m in msg for m in _RETRYABLE_MARKERS)


def _retry_loop(what, fn, attempts, backoff, tag, budget=None):
    """通用重试：只对瞬时故障重试，其余立即抛出。

    budget 耗尽后不再重试，直接抛出——宁可如实报错，也不能把定时任务拖到超时
    （超时会把可控的失败变成新告警）。
    """
    total = RETRY_BUDGET if budget is None else budget
    started = time.time()
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            fn()
            if attempt > 1:
                print(f'{tag}: {what} 第 {attempt} 次成功', flush=True)
            return
        except Exception as exc:  # noqa: BLE001
            if not is_transient(exc):
                raise
            last_error = exc
            if attempt >= attempts:
                break
            wait = min(backoff * (2 ** (attempt - 1)), 30)
            left = min(total - (time.time() - started), _remaining_budget())
            if left <= wait:
                print(f'{tag}: {what} 重试预算已用尽（剩余 {max(left, 0):.0f}s），停止重试',
                      flush=True)
                break
            print(f'{tag}: {what} 第 {attempt} 次遇瞬时故障'
                  f'（{type(exc).__name__}: {str(exc)[:90]}），{wait:.0f}s 后重试',
                  flush=True)
            time.sleep(wait)
    raise last_error


def login_with_retry(platform_obj, name, attempts=None, backoff=None):
    """带重试的登录。仅对瞬时网络错误重试，凭据/配置错误立即失败。"""
    _retry_loop(f'{name} 登录', platform_obj.login,
                attempts or LOGIN_ATTEMPTS, backoff or LOGIN_BACKOFF, 'LOGIN_RETRY')


def sync_with_retry(export_platform, export_platform_obj, import_targets,
                    attempts=None, backoff=None):
    """同步一整轮。瞬时网络抖动时重试整轮（幂等：游标推进 + 去重跳过）。"""
    _retry_loop(
        '同步本轮',
        lambda: sync_activities(export_platform, export_platform_obj, import_targets),
        attempts or SYNC_ATTEMPTS, backoff or SYNC_BACKOFF, 'SYNC_RETRY')


def _cursor_path(export_platform):
    return './.' + export_platform + '.tmp'


def _cursor_json_path(export_platform):
    return './.' + export_platform + '.cursor.json'


def get_last_sync_id(export_platform):
    """兼容旧接口：返回历史记录过的最大 ride_id（仅用于展示/兼容）。"""
    file_path = _cursor_path(export_platform)
    if os.path.exists(file_path):
        with open(file_path, 'r') as file:
            try:
                return int(file.read())
            except ValueError:
                return 0
    return 0


def set_last_sync_id(export_platform, activity_id):
    file_path = _cursor_path(export_platform)
    with open(file_path, 'w') as file:
        file.write(str(activity_id))


def load_sync_cursor(export_platform):
    """读同步水位的结构化记录。

    为什么不能只用 ride_id 当游标：
    2026-11-05 实测 iGPSPORT 的 rideId 并不按时间单调递增：
        11-02 → 59170866
        11-04 → 59170864  （比 11-02 还小）
    旧逻辑 `ride_id <= 游标 → 跳过` 把游标推到 59170866 后，
    11-04 的记录就永远被当成旧数据，再也不会被同步。

    改为：日期水位（只往下不回头）+ 已同步 ride_id 集合（防重复上传）。
    """
    path = _cursor_json_path(export_platform)
    data = {'synced_ids': [], 'last_date': ''}
    if os.path.exists(path):
        try:
            with open(path, encoding='utf-8') as f:
                loaded = json.load(f) or {}
            data['synced_ids'] = [str(x) for x in (loaded.get('synced_ids') or [])]
            data['last_date'] = str(loaded.get('last_date') or '')
        except (OSError, ValueError):
            pass
    if not data['last_date']:
        # 首次升级：用旧游标对应的活动日期做起点，避免把历史全部重传
        legacy = get_last_sync_id(export_platform)
        if legacy:
            data['last_date'] = _legacy_cursor_date_hint()
    return data


def _legacy_cursor_date_hint():
    """旧游标没有日期信息，保守起见回退到「最近 7 天」作为水位。"""
    return (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')


def save_sync_cursor(export_platform, cursor):
    path = _cursor_json_path(export_platform)
    cursor['synced_ids'] = cursor.get('synced_ids', [])[-500:]
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cursor, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    # 保留旧文件，供排查与兼容
    ids = [int(x) for x in cursor['synced_ids'] if str(x).isdigit()]
    if ids:
        set_last_sync_id(export_platform, max(ids))


def build_platform_list():
    return {
        'igpsport': igpsport(),
        'imxingzhe': imxingzhe(),
        'intervalsicu': intervalsicu(),
    }


def _activity_date(activity):
    raw = activity.get('origin_activity') or activity
    value = (raw.get('startTime') or raw.get('start_time') or raw.get('recordTime') or
             raw.get('record_time') or raw.get('date') or activity.get('title') or '')
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value / 1000 if value > 10**11 else value)
    match = re.search(r'(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})', str(value))
    return '{}-{:02d}-{:02d}'.format(match.group(1), int(match.group(2)), int(match.group(3))) if match else ''


def _activity_distance_km(activity):
    raw = activity.get('origin_activity') or activity
    value = raw.get('rideDistance') or raw.get('distance') or raw.get('totalDistance') or 0
    try:
        value = float(value)
        return value / 1000 if value > 500 else value
    except (TypeError, ValueError):
        return None


def _activity_time(activity):
    raw = activity.get('origin_activity') or activity
    value = raw.get('start_time') or raw.get('startTime') or raw.get('record_time')
    if isinstance(value, (int, float)):
        return value / 1000 if value > 10**11 else value
    if value:
        for fmt in ('%Y.%m.%d %H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y.%m.%dT%H:%M:%S'):
            try:
                return datetime.strptime(str(value), fmt).timestamp()
            except ValueError:
                pass
    return None


def is_duplicate_activity(activity, target_activities, tolerance_km=2.0, tolerance_minutes=30):
    """按日期、里程及（接口提供时）开始时间去重，不依赖平台活动 ID。

    不同平台的里程算法可能相差 1--2 km；开始时间可用时，要求时间也接近，
    避免把同一天的两次短途骑行误判成同一条。
    """
    date = _activity_date(activity)
    distance = _activity_distance_km(activity)
    if not date or distance is None:
        return False
    for target in target_activities:
        if _activity_date(target) != date:
            continue
        target_distance = _activity_distance_km(target)
        if target_distance is None or abs(distance - target_distance) > tolerance_km:
            continue
        source_time, target_time = _activity_time(activity), _activity_time(target)
        if source_time is not None and target_time is not None:
            if abs(source_time - target_time) > tolerance_minutes * 60:
                continue
            return True
        return True  # 活动列表只有日期时，以日期+里程作为兼容判断
    return False


def _parse_import_platforms():
    """解析 IMPORT_PLATFORM，支持逗号分隔的多个目标（例如 imxingzhe,intervalsicu）。"""
    raw = os.getenv('IMPORT_PLATFORM') or ''
    return [name.strip() for name in raw.split(',') if name.strip()]


def _resolve_file_format(export_platform, import_platform):
    """按「导出→导入」这一对决定文件格式；显式配置优先。"""
    fmt = os.getenv('SYNC_FILE_FORMAT', '').strip().lower().lstrip('.')
    if not fmt:
        fmt = 'gpx' if (export_platform, import_platform) == ('imxingzhe', 'igpsport') else 'fit'
    if fmt not in ('fit', 'gpx', 'tcx'):
        raise Exception('SYNC_FILE_FORMAT 只支持 fit、gpx 或 tcx')
    return fmt


def sync_activities(export_platform, export_platform_obj, import_targets, file_format=None):
    """把导出平台的新活动同步到全部目标平台。

    import_targets 为 [(名称, 平台对象), ...]。游标语义是「已同步到所有目标」：
    只有一条活动在所有目标都处理完（上传成功，或该目标已存在）之后才推进游标。
    任一目标抛错就中断整轮且不推进该条游标，下次重跑时已成功的目标靠去重跳过，
    失败的目标重试——不会丢数据。
    """
    # 兼容旧的单目标调用方式。
    if not isinstance(import_targets, (list, tuple)):
        import_targets = [('target', import_targets)]

    last_sync_id = get_last_sync_id(export_platform)
    cursor = load_sync_cursor(export_platform)
    synced_ids = {str(x) for x in cursor.get('synced_ids', [])}
    last_date = cursor.get('last_date', '') or ''
    activity_list = export_platform_obj.get_activity_list()
    dedup_tolerance = float(os.getenv('DEDUP_TOLERANCE_KM', '2.0'))
    # 每个目标各自持有一份既有活动列表用于去重，互不干扰。
    target_activities = {}
    for name, target_obj in import_targets:
        target_activities[name] = target_obj.get_activity_list()

    synced_count = 0
    skipped_count = 0
    synced_titles = []
    newly_synced = []
    max_date_seen = last_date
    for activity in activity_list:
        ride_id = str(activity['ride_id'])
        act_date = _activity_date(activity)
        # 双重判定：
        # 1) 这条 ride_id 历史上已成功处理过 → 跳过（防重复上传）
        # 2) 日期早于水位 → 跳过（回溯保护）
        # 注意：不能再用 ride_id 大小比较，iGPSPORT 的 id 不单调（见 load_sync_cursor 注释）。
        if ride_id in synced_ids:
            continue
        # 日期水位带「回溯窗口」：iGPSPORT 允许多天后才补传某天的骑行，
        # 若把水位当成硬门槛，迟到的旧日期记录会被永久跳过
        # （2026-11-05 就是 11-02/11-04 一次性补传时撞上的）。
        # 这里只跳过「明显过旧」的记录，防的是首次接入时把 2022 年起的历史全部重传。
        if last_date and act_date:
            lookback_days = int(os.getenv('SYNC_LOOKBACK_DAYS', '7') or 7)
            try:
                cutoff = (datetime.strptime(last_date, '%Y-%m-%d')
                          - timedelta(days=lookback_days)).strftime('%Y-%m-%d')
            except ValueError:
                cutoff = ''
            if cutoff and act_date < cutoff:
                continue
        uploaded_to = []
        for name, target_obj in import_targets:
            if is_duplicate_activity(activity, target_activities[name], dedup_tolerance):
                continue
            extension = file_format or _resolve_file_format(export_platform, name)
            extension = str(extension).lower().lstrip('.')
            export_method = getattr(export_platform_obj, 'export_' + extension)
            file_data = export_method(activity['origin_activity'])
            import_method = getattr(target_obj, 'import_file', None)
            if import_method:
                import_method(file_data, activity['title'], '.' + extension)
            else:
                target_obj.import_fit(file_data, activity['title'])
            target_activities[name].append({'title': activity['title'],
                                            'origin_activity': activity['origin_activity']})
            uploaded_to.append(name)
        # 该条在所有目标都处理完，才记为已处理。
        synced_ids.add(ride_id)
        newly_synced.append(ride_id)
        last_sync_id = activity['ride_id']
        if act_date and act_date > max_date_seen:
            max_date_seen = act_date
        cursor['synced_ids'] = list(synced_ids)
        cursor['last_date'] = max_date_seen
        save_sync_cursor(export_platform, cursor)
        if uploaded_to:
            synced_count += 1
            synced_titles.append(activity['title'])
        else:
            skipped_count += 1
    print(f'SYNC_RESULT: synced={synced_count} skipped_duplicates={skipped_count} titles={synced_titles}')
    return synced_count, synced_titles


def check_differences(export_platform_obj, import_targets, export_name=None):
    """只读列出每个目标与源之间按标题归一化后的独有记录，不写入状态文件。"""
    if not isinstance(import_targets, (list, tuple)):
        import_targets = [('target', import_targets)]
    source = export_platform_obj.get_activity_list()
    tolerance = float(os.getenv('DEDUP_TOLERANCE_KM', '2.0'))
    left = export_name or 'source'
    results = []
    for name, target_obj in import_targets:
        target = target_obj.get_activity_list()
        source_only = [x['title'] for x in source if not is_duplicate_activity(x, target, tolerance)]
        target_only = [x['title'] for x in target if not is_duplicate_activity(x, source, tolerance)]
        print('DIFF_RESULT: {}_only={} {}_only={}'.format(left, source_only, name, target_only))
        results.append((source_only, target_only))
    return results


def should_run_once():
    return os.getenv('RUN_FOREVER') != 'True'


def main(run_once=None):
    load_dotenv()
    if run_once is None:
        run_once = should_run_once()

    export_platform = os.getenv('EXPORT_PLATFORM')
    import_platforms = _parse_import_platforms()
    platform_list = build_platform_list()
    if export_platform not in platform_list:
        raise Exception('导出平台配置不正确')
    if not import_platforms:
        raise Exception('导入平台配置不正确')
    for name in import_platforms:
        if name not in platform_list:
            raise Exception('导入平台配置不正确：' + name)
    if len(set(import_platforms)) != len(import_platforms):
        raise Exception('导入平台重复：' + ','.join(import_platforms))

    # 提前校验格式，避免同步到一半才因为配置错误中断。
    explicit_format = os.getenv('SYNC_FILE_FORMAT', '').strip().lower().lstrip('.')
    if explicit_format and explicit_format not in ('fit', 'gpx', 'tcx'):
        raise Exception('SYNC_FILE_FORMAT 只支持 fit、gpx 或 tcx')

    export_platform_obj = platform_list[export_platform]
    import_targets = [(name, platform_list[name]) for name in import_platforms]
    # 平台初始化 如果账户密码错误抛出异常
    # 登录阶段的瞬时断连（RemoteDisconnected 等）重试，避免偶发抖动导致整轮失败。
    login_with_retry(export_platform_obj, export_platform)
    for _name, target_obj in import_targets:
        login_with_retry(target_obj, _name)
    if os.getenv('CHECK_ONLY', '').lower() in ('1', 'true', 'yes'):
        check_differences(export_platform_obj, import_targets, export_platform)
        return
    if (export_platform == 'imxingzhe' and 'igpsport' in import_platforms
            and os.getenv('SYNC_CONFIRM') != 'YES'):
        raise Exception('行者→iGPSPORT 同步需要明确设置 SYNC_CONFIRM=YES；可先设置 CHECK_ONLY=True 只读检查')
    # 开始同步
    while True:
        sync_with_retry(export_platform, export_platform_obj, import_targets)
        if run_once:
            break
        time.sleep(60)


if __name__ == '__main__':
    main()
