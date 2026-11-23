#!/usr/bin/env python
# -*- coding: utf-8 -*一
import os

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class igpsport:
    BASE_URL = 'https://prod.zh.igpsport.com/service'
    # 建连超时缩到 10s，并配合 session 上的连接级重试：
    # 网络/TLS 抖动时快速重试，而不是白等 30s（甚至 120s）后整轮失败。
    TIMEOUT = (10, 60)
    TIMEOUT_TRANSFER = (10, 120)

    def __init__(self):
        self.session = requests.session()
        self.session.verify = False
        self.session.trust_env = False
        # 只重试「连接失败」（read=0/status=0），所以连上传（PUT/POST）也能安全重试：
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
        if os.getenv('DEBUG_MODE') == 'True':
            self.session.trust_env = True
            self.session.proxies = {
                'http': '127.0.0.1:8888',
                'https': '127.0.0.1:8888'
            }

    def _headers(self):
        headers = {
            'Accept': 'application/json, text/plain, */*',
            'Origin': 'https://app.igpsport.cn',
            'Referer': 'https://app.igpsport.cn/',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
        }
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        return headers

    def _safe_int(self, value, default=0):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def login(self):
        url = self.BASE_URL + '/auth/account/login'
        params = {
            'username': os.getenv('IGPSPORT_USERNAME'),
            'password': os.getenv('IGPSPORT_PASSWORD'),
            'appId': 'igpsport-web',
        }
        response = self.session.post(
            url,
            json=params,
            headers={**self._headers(), 'Content-Type': 'application/json;charset=UTF-8'},
            timeout=self.TIMEOUT
        )
        content = response.json()
        if content.get('code') != 0:
            raise Exception('迹驰登陆失败：' + str(content.get('message', response.text)))
        self.token = (content.get('data') or {}).get('access_token')
        if not self.token:
            raise Exception('迹驰登陆失败：未获取到 access_token')

    def get_activity_list(self):
        if not self.token:
            raise Exception('迹驰获取活动列表失败：未登录')

        activity_list = []
        page = 1
        total_page = 1
        while page <= total_page:
            url = self.BASE_URL + '/web-gateway/web-analyze/activity/queryMyActivity'
            params = {
                'pageNo': page,
                'pageSize': 20,
                'reqType': 0,
                'sort': 1,
            }
            response = self.session.get(url, params=params, headers=self._headers(), timeout=self.TIMEOUT)
            content = response.json()
            if content.get('code') != 0:
                raise Exception('迹驰获取活动列表失败：' + str(content.get('message', response.text)))
            data = content.get('data') or {}
            rows = data.get('rows') or []
            total_page = self._safe_int(data.get('totalPage'), 1)
            for activity in rows:
                ride_id = self._safe_int(activity.get('rideId'))
                start_time = str(activity.get('startTime') or '').replace('.', '-')
                title = str(activity.get('title') or '').strip()
                display_title = (start_time + title) if title else (start_time + '的骑行')
                activity_list.append({
                    'ride_id': ride_id,
                    'title': display_title,
                    'origin_activity': activity,
                })
            if not rows:
                break
            page += 1

        activity_list.reverse()
        return activity_list

    def export_fit(self, activity):
        if not self.token:
            raise Exception('迹驰导出fit文件失败：未登录')
        ride_id = self._safe_int(activity.get('rideId') or activity.get('RideId'))
        if not ride_id:
            raise Exception('迹驰导出fit文件失败：缺少 rideId')

        url = self.BASE_URL + '/web-gateway/web-analyze/activity/getDownloadUrl/' + str(ride_id)
        response = self.session.get(url, headers=self._headers(), timeout=self.TIMEOUT)
        content = response.json()
        if content.get('code') != 0:
            raise Exception('迹驰获取fit下载地址失败：' + str(content.get('message', response.text)))
        download_url = content.get('data')
        if not download_url:
            raise Exception('迹驰获取fit下载地址失败：下载地址为空')

        response = self.session.get(download_url, headers=self._headers(), timeout=self.TIMEOUT_TRANSFER)
        content = response.content
        if response.status_code != 200 or not content:
            raise Exception('迹驰导出fit文件失败：' + str(ride_id))
        return content

    def import_fit(self, fit_file, title):
        return self.import_file(fit_file, title, '.fit')

    def import_file(self, file_data, title, extension='.fit'):
        if not self.token:
            raise Exception('迹驰上传文件失败：未登录')
        ext = extension if str(extension).startswith('.') else '.' + str(extension)
        ext = ext.lower()
        if ext not in ('.fit', '.gpx', '.tcx'):
            raise Exception('迹驰不支持的文件格式：' + ext)
        signed = self.session.get(self.BASE_URL + '/sportg/third-party-server/oss/getSignedUrl',
                                  params={'fileExtension': ext}, headers=self._headers(), timeout=self.TIMEOUT).json()
        if signed.get('code', 0) not in (0, None):
            raise Exception('迹驰获取上传地址失败：' + str(signed))
        data = signed.get('data') or signed
        signed_url, oss_id = data.get('signedUrl'), data.get('ossId')
        if not signed_url or not oss_id:
            raise Exception('迹驰获取上传地址失败：返回值不完整')
        response = self.session.put(signed_url, data=file_data, headers={'Content-Type': ''}, timeout=self.TIMEOUT_TRANSFER)
        if response.status_code >= 300:
            raise Exception('迹驰上传文件到 OSS 失败：HTTP ' + str(response.status_code))
        result = self.session.post(self.BASE_URL + '/web-gateway/web-analyze/activity/uploadByOss',
                                   json={'fileName': str(title)[:50] + ext, 'ossName': oss_id},
                                   headers={**self._headers(), 'Content-Type': 'application/json;charset=UTF-8'},
                                   timeout=self.TIMEOUT).json()
        if result.get('code', 0) not in (0, None):
            raise Exception('迹驰提交 FIT 解析失败：' + str(result))
