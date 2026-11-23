#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
from datetime import datetime, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class intervalsicu:
    """intervals.icu 导入端：上传 FIT/GPX/TCX，并读回活动列表用于去重。

    鉴权用个人 API key（Basic，用户名固定 `API_KEY`，密码为 API key）。
    athlete id 用 '0' 表示「API key 所属的运动员」，因此不必手填真实数字 id。
    参考 https://intervals.icu/api/v1/docs 的 OpenAPI 规范。
    """
    BASE_URL = 'https://intervals.icu'
    ATHLETE = '0'
    # intervals.icu 会拒绝没有 User-Agent 的请求（403）。实测：不带 UA 时
    # /api/v1/athlete/0 返回 403，带上即 200。requests 默认有 UA，但保险起见显式声明。
    USER_AGENT = 'ride-sync/1.0'
    # 与 igpsport.py / imxingzhe.py 同构：建连超时 10s，配合连接级重试。
    TIMEOUT = (10, 60)
    TIMEOUT_TRANSFER = (10, 120)

    def __init__(self):
        self.session = requests.session()
        self.session.trust_env = False
        # 只重试「连接失败」（read=0/status=0）。连接没建立起来时请求没到达服务端，
        # 所以连 POST 上传也能安全重试，不会产生重复活动。
        retry = Retry(
            total=4,
            connect=4,
            read=0,
            status=0,
            backoff_factor=1.0,
            allowed_methods=frozenset(['GET', 'POST', 'PUT', 'DELETE', 'HEAD', 'OPTIONS']),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount('https://', adapter)
        self.session.mount('http://', adapter)
        self.session.headers.update({
            'User-Agent': self.USER_AGENT,
            'Accept': 'application/json',
        })
        self.logged_in = False
        self.athlete_id = os.getenv('INTERVALS_ICU_ATHLETE_ID') or self.ATHLETE
        # 配置在构造时读一次并持有；login() 只校验，不再重新读环境。
        self.api_key = os.getenv('INTERVALS_ICU_API_KEY') or ''
        if self.api_key:
            self.session.auth = ('API_KEY', self.api_key)
        if os.getenv('DEBUG_MODE') == 'True':
            self.session.trust_env = True
            self.session.proxies = {
                'http': 'http://127.0.0.1:8888',
                'https': 'http://127.0.0.1:8888',
            }

    def _athlete_url(self, suffix=''):
        return self.BASE_URL + '/api/v1/athlete/' + str(self.athlete_id) + suffix

    @staticmethod
    def _message(response, fallback):
        try:
            content = response.json()
        except ValueError:
            return str(response.text or fallback)
        if isinstance(content, dict):
            for key in ('message', 'error', 'detail', 'title'):
                if content.get(key):
                    return str(content[key])
        return str(fallback)

    @staticmethod
    def _numeric_id(raw):
        """ICU 活动 id 形如 'i12345678'，游标比较需要纯数字。"""
        digits = ''.join(ch for ch in str(raw or '') if ch.isdigit())
        return int(digits) if digits else 0

    def login(self):
        """用一次只读请求校验 API key，坏凭据在这里就失败，不带到上传阶段。"""
        if not self.api_key:
            raise Exception('intervals.icu 未配置 INTERVALS_ICU_API_KEY')
        response = self.session.get(
            self._athlete_url(),
            timeout=self.TIMEOUT,
        )
        if response.status_code == 401:
            raise Exception('intervals.icu 鉴权失败：API key 无效或已被撤销')
        if response.status_code >= 400:
            raise Exception('intervals.icu 连接失败：HTTP ' + str(response.status_code) +
                            ' ' + self._message(response, ''))
        self.logged_in = True

    def get_activity_list(self):
        """读取近期活动用于去重；返回升序列表以匹配 main.py 的游标语义。

        ICU 无法通过 API 转发来源为 Strava 的活动，但这里读的是「活动摘要」，
        用于比对是否已存在，因此不受影响。
        """
        if not self.logged_in:
            raise Exception('intervals.icu 获取活动列表失败：未登录')
        days = int(os.getenv('INTERVALS_ICU_LOOKBACK_DAYS', '90'))
        newest = datetime.utcnow()
        oldest = newest - timedelta(days=days)
        response = self.session.get(
            self._athlete_url('/activities'),
            params={
                'oldest': oldest.strftime('%Y-%m-%dT%H:%M:%S'),
                'newest': newest.strftime('%Y-%m-%dT%H:%M:%S'),
            },
            timeout=self.TIMEOUT,
        )
        if response.status_code >= 400:
            raise Exception('intervals.icu 获取活动列表失败：HTTP ' + str(response.status_code) +
                            ' ' + self._message(response, ''))
        rows = response.json() or []
        activities = [self._normalize(row) for row in rows]
        activities.reverse()  # 接口按时间倒序，转升序
        return activities

    @staticmethod
    def _parse_time(raw):
        """把 ICU 的 ISO 时间（带 T）转成 main.py 去重逻辑认得的 'YYYY-MM-DD HH:MM:SS'。

        main.py 的 _activity_time 只尝试空格分隔的格式，直接传 ISO 会解析失败、
        静默退化成「只比日期+里程」。这里对齐格式，让开始时间参与判定。
        """
        text = str(raw or '').strip().replace('T', ' ')
        if not text:
            return ''
        # 去掉可能存在的毫秒/时区尾巴，只保留到秒
        if '.' in text:
            head, _, tail = text.partition('.')
            # 时区可能跟在秒后（如 07:30:00+08:00），保留 offset 交给上游
            text = head + ('+' + tail.split('+', 1)[1] if '+' in tail else '')
        for fmt in ('%Y-%m-%d %H:%M:%S%z', '%Y-%m-%d %H:%M:%S'):
            try:
                return datetime.strptime(text.strip(), fmt).strftime('%Y-%m-%d %H:%M:%S')
            except ValueError:
                continue
        return text[:19]

    @classmethod
    def _normalize(cls, row):
        raw_id = row.get('id')
        start = cls._parse_time(row.get('start_date_local') or row.get('start_date'))
        title = str(row.get('name') or '').strip() or (start[:10] + ' 骑行')
        return {
            'ride_id': cls._numeric_id(raw_id),
            'title': title,
            # 键名对齐 main.py 去重逻辑读取的字段：start_time 提供日期，
            # distance 提供里程（ICU 单位为米，与 igpsport 一致）。
            'origin_activity': {
                'id': raw_id,
                'name': title,
                'start_time': start,
                'distance': row.get('distance'),
                'source': row.get('source'),
                'strava_id': row.get('strava_id'),
            },
        }

    def import_file(self, file_data, title, extension='.fit'):
        if not self.logged_in:
            raise Exception('intervals.icu 上传失败：未登录')
        ext = extension if str(extension).startswith('.') else '.' + str(extension)
        ext = ext.lower()
        if ext not in ('.fit', '.gpx', '.tcx'):
            raise Exception('intervals.icu 不支持的文件格式：' + ext)
        name = str(title)[:100]
        response = self.session.post(
            self._athlete_url('/activities'),
            params={'name': name},
            files={'file': (name + ext, file_data, 'application/octet-stream')},
            timeout=self.TIMEOUT_TRANSFER,
        )
        if response.status_code >= 400:
            raise Exception('intervals.icu 上传失败：HTTP ' + str(response.status_code) +
                            ' ' + self._message(response, ''))

    def import_fit(self, fit_file, title):
        return self.import_file(fit_file, title, '.fit')
