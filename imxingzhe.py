#!/usr/bin/env python
# -*- coding: utf-8 -*-
import base64
import hashlib
import os

import requests
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA


class imxingzhe:
    BASE_URL = 'https://www.imxingzhe.com'
    PUBLIC_KEY = (
        'MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDmuQkBbijudDAJgfffDeeIButqWHZvUwcRuvWdg89393FS'
        'dz3IJUHc0rgI/S3WuU8N0VePJLmVAZtCOK4qe4FY/eKmWpJmn7JfXB4HTMWjPVoyRZmSYjW4L8GrWmh51Qj'
        '7DwpTADadF3aq04o+s1b8LXJa8r6+TIqqL5WUHtRqmQIDAQAB'
    )

    def __init__(self):
        self.session = requests.session()
        self.session.verify = False
        self.session.trust_env = False
        self.token = None
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
            timeout=30
        )
        content = response.json()
        if response.status_code >= 400 or content.get('code') not in (0, None):
            raise Exception('行者登陆失败：' + self._message_from_response(content, response.text))
        self.token = self._extract_token(content, response)
        if not self.token:
            raise Exception('行者登陆失败：未获取到 access_token')

    def get_activity_list(self):
        pass

    def export_fit(self, ride_id):
        pass

    def import_fit(self, fit_file, title):
        file_source_list = {
            'igpsport': 'iGPSPORT',
            'onelap': 'Onelap',
        }
        export_platform = os.getenv('EXPORT_PLATFORM')
        if export_platform not in file_source_list:
            raise Exception('暂不支持导入该平台数据')
        if not self.token:
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
        response = self.session.post(url, data=params, files=files, headers=self._headers(), timeout=120)
        content = response.json()
        if content.get('code') in (0, 9006, 9007):
            return
        raise Exception('上传轨迹失败：' + self._message_from_response(content, response.text))
