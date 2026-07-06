#!/usr/bin/env python
# -*- coding: utf-8 -*一
import os

import requests


class igpsport:
    BASE_URL = 'https://prod.zh.igpsport.com/service'

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
            timeout=30
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
            response = self.session.get(url, params=params, headers=self._headers(), timeout=30)
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
        response = self.session.get(url, headers=self._headers(), timeout=30)
        content = response.json()
        if content.get('code') != 0:
            raise Exception('迹驰获取fit下载地址失败：' + str(content.get('message', response.text)))
        download_url = content.get('data')
        if not download_url:
            raise Exception('迹驰获取fit下载地址失败：下载地址为空')

        response = self.session.get(download_url, headers=self._headers(), timeout=120)
        content = response.content
        if response.status_code != 200 or not content:
            raise Exception('迹驰导出fit文件失败：' + str(ride_id))
        return content

    def import_fit(self, fit_file, title):
        pass
