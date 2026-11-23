#!/usr/bin/env python
# -*- coding: utf-8 -*-
import base64
import hashlib
import os

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA


class imxingzhe:
    BASE_URL = 'https://www.imxingzhe.com'
    GPX_PATH = '/api/v1/workout/{}/gpx/'
    # 建连超时缩到 10s（原来是 30s），并配合 session 上的连接级重试：
    # 网络抖动时快速重试，而不是白等 30s 直接失败。
    TIMEOUT = (10, 60)
    TIMEOUT_TRANSFER = (10, 120)
    PUBLIC_KEY = (
        'MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDmuQkBbijudDAJgfffDeeIButqWHZvUwcRuvWdg89393FS'
        'dz3IJUHc0rgI/S3WuU8N0VePJLmVAZtCOK4qe4FY/eKmWpJmn7JfXB4HTMWjPVoyRZmSYjW4L8GrWmh51Qj'
        '7DwpTADadF3aq04o+s1b8LXJa8r6+TIqqL5WUHtRqmQIDAQAB'
    )

    def __init__(self):
        self.session = requests.session()
        self.session.verify = False
        self.session.trust_env = False
        # 只重试「连接失败」（read=0/status=0），所以连 POST 上传也能安全重试：
        # 连接都没建立起来时，请求根本没有到达服务端，不会产生重复数据。
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
        self.token = None
        self.logged_in = False
        env_token = self._normalize_token(os.getenv('IMXINGZHE_TOKEN'))
        if env_token:
            self.token = env_token
            self.logged_in = True
        if os.getenv('DEBUG_MODE') == 'True':
            self.session.trust_env = True
            self.session.proxies = {
                'http': '127.0.0.1:8888',
                'https': '127.0.0.1:8888'
            }

    def _headers(self):
        headers = {
            'Accept': 'application/json, text/plain, */*',
            'Origin': self.BASE_URL,
            'Referer': self.BASE_URL + '/login',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
        }
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        cookie = os.getenv('IMXINGZHE_COOKIE')
        if cookie:
            headers['Cookie'] = cookie
        return headers

    def _encrypt_password(self, password):
        recipient_key = RSA.import_key(base64.b64decode(self.PUBLIC_KEY))
        cipher = PKCS1_v1_5.new(recipient_key)
        return base64.b64encode(cipher.encrypt(password.encode('utf-8'))).decode()

    def _normalize_token(self, token):
        if not token:
            return None
        token = str(token).strip()
        if token.lower().startswith('bearer '):
            token = token[7:].strip()
        token = token.split('&')[0].strip()
        return token or None

    def _extract_token(self, content, response=None):
        data = content.get('data') or {}
        token_keys = (
            'access_token', 'accessToken', 'assess_token', 'assessToken',
            'token', 'access', 'Authorization', 'authorization'
        )
        for source in (data, content):
            for key in token_keys:
                token = self._normalize_token(source.get(key))
                if token:
                    return token

        cookie_names = (
            '_XingzheWeb_AssessToken', 'XingzheWeb_AssessToken',
            '_XingzheWeb_AccessToken', 'XingzheWeb_AccessToken'
        )
        for cookie_source in (getattr(response, 'cookies', None), getattr(self.session, 'cookies', None)):
            if not cookie_source:
                continue
            for name in cookie_names:
                getter = getattr(cookie_source, 'get', None)
                token = self._normalize_token(getter(name) if getter else None)
                if token:
                    return token
        return None

    def _message_from_response(self, content, fallback):
        return str(content.get('message') or content.get('error_message') or content.get('msg') or fallback)

    def login(self):
        params = {
            'account': os.getenv('IMXINGZHE_USERNAME'),
            'password': self._encrypt_password(os.getenv('IMXINGZHE_PASSWORD') or ''),
        }
        url = self.BASE_URL + '/api/v1/user/login/'
        response = self.session.post(
            url,
            json=params,
            headers={**self._headers(), 'Content-Type': 'application/json;charset=UTF-8'},
            timeout=self.TIMEOUT
        )
        content = response.json()
        if response.status_code >= 400 or content.get('code') not in (0, None):
            raise Exception('行者登陆失败：' + self._message_from_response(content, response.text))
        self.token = self._extract_token(content, response)
        self.logged_in = True

    def get_activity_list(self):
        if not self.logged_in and not self.token and not self.session.cookies:
            raise Exception('行者获取活动列表失败：未登录')
        rows, offset, limit = [], 0, 100
        while True:
            response = self.session.get(self.BASE_URL + '/api/v1/pgworkout/',
                                        params={'offset': offset, 'limit': limit},
                                        headers=self._headers(), timeout=self.TIMEOUT)
            content = response.json()
            if response.status_code >= 400:
                raise Exception('行者获取活动列表失败：' + self._message_from_response(content, response.text))
            data = content.get('data') if isinstance(content, dict) else content
            if isinstance(data, dict):
                page = (data.get('results') or data.get('items') or data.get('workouts') or
                        data.get('list') or data.get('data') or [])
            else:
                page = data or []
            if not page:
                break
            for activity in page:
                workout_id = activity.get('id') or activity.get('workout_id') or activity.get('workoutId')
                if workout_id is None:
                    continue
                title = str(activity.get('title') or activity.get('name') or ('workout_' + str(workout_id))).strip()
                rows.append({'ride_id': int(workout_id) if str(workout_id).isdigit() else str(workout_id),
                             'title': title, 'origin_activity': activity})
            if len(page) < limit:
                break
            offset += len(page)
        rows.reverse()
        return rows

    def export_fit(self, ride_id):
        workout_id = ride_id.get('ride_id') if isinstance(ride_id, dict) else ride_id
        if isinstance(ride_id, dict):
            workout_id = ride_id.get('id') or ride_id.get('workout_id') or workout_id
        response = self.session.get(self.BASE_URL + '/api/v1/workout/{}/fit/'.format(workout_id),
                                    headers=self._headers(), timeout=self.TIMEOUT_TRANSFER)
        if response.status_code >= 400 or not response.content:
            raise Exception('行者导出 FIT 失败：' + str(workout_id))
        return response.content

    def export_gpx(self, ride_id):
        """导出网页端可下载的 GPX；该接口不要求 FIT 会员权限。"""
        workout_id = ride_id.get('ride_id') if isinstance(ride_id, dict) else ride_id
        if isinstance(ride_id, dict):
            workout_id = ride_id.get('id') or ride_id.get('workout_id') or workout_id
        response = self.session.get(self.BASE_URL + self.GPX_PATH.format(workout_id),
                                    headers=self._headers(), timeout=self.TIMEOUT_TRANSFER)
        if response.status_code >= 400 or not response.content:
            raise Exception('行者导出 GPX 失败：' + str(workout_id))
        content = response.content
        # 该接口实际返回 JSON 字符串（内容首尾带引号），需先还原 XML。
        if content[:1] == b'"':
            try:
                import json
                decoded = response.json()
                if isinstance(decoded, str):
                    content = decoded.encode('utf-8')
            except (ValueError, UnicodeDecodeError):
                pass
        if not content.lstrip().startswith(b'<?xml') and b'<gpx' not in content[:500]:
            raise Exception('行者导出 GPX 失败：返回内容不是 GPX')
        return content

    def import_fit(self, fit_file, title):
        file_source_list = {
            'igpsport': 'iGPSPORT',
        }
        export_platform = os.getenv('EXPORT_PLATFORM')
        if export_platform not in file_source_list:
            raise Exception('暂不支持导入该平台数据')
        if not self.logged_in:
            raise Exception('上传轨迹失败：未登录')

        fit_filename = title + '.fit'
        files = {
            'fit_file': (fit_filename, fit_file, 'application/octet-stream')
        }
        params = {
            'file_source': file_source_list[export_platform],
            'fit_filename': fit_filename,
            'md5': hashlib.md5(fit_file).hexdigest(),
            'name': title,
            'sport': 3,
        }

        url = self.BASE_URL + '/api/v1/fit/upload/'
        response = self.session.post(url, data=params, files=files, headers=self._headers(), timeout=self.TIMEOUT_TRANSFER)
        content = response.json()
        if content.get('code') in (0, 9006, 9007):
            return
        raise Exception('上传轨迹失败：' + self._message_from_response(content, response.text))
