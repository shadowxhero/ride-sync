import os
import unittest
from unittest.mock import patch

from igpsport import igpsport


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, content=b''):
        self.status_code = status_code
        self._json_data = json_data
        self.content = content
        self.text = str(json_data) if json_data is not None else content.decode('utf-8', errors='ignore')

    def json(self):
        if isinstance(self._json_data, Exception):
            raise self._json_data
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'HTTP {self.status_code}')


class FakeSession:
    def __init__(self):
        self.verify = True
        self.trust_env = True
        self.proxies = {}
        self.requests = []

    def post(self, url, **kwargs):
        self.requests.append(('POST', url, kwargs))
        if url.endswith('/auth/account/login'):
            return FakeResponse(json_data={
                'code': 0,
                'data': {
                    'access_token': 'token-123',
                },
            })
        raise AssertionError(f'unexpected POST {url}')

    def get(self, url, **kwargs):
        self.requests.append(('GET', url, kwargs))
        if 'queryMyActivity' in url:
            return FakeResponse(json_data={
                'code': 0,
                'data': {
                    'rows': [
                        {
                            'rideId': 42,
                            'title': 'morning_1717214400_ride',
                            'startTime': '2024.06.01',
                            'rideDistance': 12345,
                            'totalMovingTime': 3600,
                        },
                    ],
                    'totalPage': 1,
                },
            })
        if url.endswith('/getDownloadUrl/42'):
            return FakeResponse(json_data={
                'code': 0,
                'data': 'https://download.example/42.fit',
            })
        if url == 'https://download.example/42.fit':
            return FakeResponse(content=b'FIT-DATA')
        raise AssertionError(f'unexpected GET {url}')


class IgpsportApiTests(unittest.TestCase):
    def test_uses_current_token_api_for_login_list_and_fit_download(self):
        session = FakeSession()
        with patch('igpsport.requests.session', return_value=session), \
             patch.dict(os.environ, {
                 'IGPSPORT_USERNAME': 'user',
                 'IGPSPORT_PASSWORD': 'pass',
             }, clear=False):
            client = igpsport()
            client.login()
            activities = client.get_activity_list()
            fit_file = client.export_fit(activities[0]['origin_activity'])

        self.assertEqual('token-123', client.token)
        self.assertEqual(b'FIT-DATA', fit_file)
        self.assertEqual(42, activities[0]['ride_id'])
        self.assertIn('/auth/account/login', session.requests[0][1])
        self.assertIn('queryMyActivity', session.requests[1][1])
        self.assertEqual('Bearer token-123', session.requests[1][2]['headers']['Authorization'])


class MainLoopTests(unittest.TestCase):
    def test_run_once_syncs_current_batch_without_sleeping_forever(self):
        import main

        class ExportPlatform:
            def __init__(self):
                self.login_called = False

            def login(self):
                self.login_called = True

            def get_activity_list(self):
                return [{
                    'ride_id': 10,
                    'title': 'ride',
                    'origin_activity': {'id': 10},
                }]

            def export_fit(self, activity):
                return b'FIT'

        class ImportPlatform:
            def __init__(self):
                self.imports = []

            def login(self):
                pass

            def import_fit(self, fit_file, title):
                self.imports.append((fit_file, title))

        export_platform = ExportPlatform()
        import_platform = ImportPlatform()
        platforms = {
            'igpsport': export_platform,
            'imxingzhe': import_platform,
        }

        with patch.dict(os.environ, {
            'EXPORT_PLATFORM': 'igpsport',
            'IMPORT_PLATFORM': 'imxingzhe',
        }, clear=False), \
             patch.object(main, 'build_platform_list', return_value=platforms), \
             patch.object(main, 'get_last_sync_id', return_value=0), \
             patch.object(main, 'set_last_sync_id') as set_last_sync_id, \
             patch.object(main.time, 'sleep', side_effect=AssertionError('should not sleep in run_once')):
            main.main(run_once=True)

        self.assertTrue(export_platform.login_called)
        self.assertEqual([(b'FIT', 'ride')], import_platform.imports)
        set_last_sync_id.assert_called_once_with('igpsport', 10)


class FakeXingzheSession:
    def __init__(self):
        self.verify = True
        self.trust_env = True
        self.proxies = {}
        self.headers = {}
        self.requests = []

    def post(self, url, **kwargs):
        self.requests.append(('POST', url, kwargs))
        if url.endswith('/api/v1/user/login/'):
            payload = kwargs.get('json') or kwargs.get('data') or {}
            if payload.get('password') == 'plain-password':
                raise AssertionError('password must be RSA encrypted before login')
            return FakeResponse(json_data={
                'code': 0,
                'data': {
                    'access_token': 'xz-token',
                    'refresh_token': 'xz-refresh',
                },
            })
        if url.endswith('/api/v1/fit/upload/'):
            headers = kwargs.get('headers') or {}
            auth = headers.get('Authorization') or self.headers.get('Authorization')
            if auth != 'Bearer xz-token':
                raise AssertionError(f'missing bearer token: {auth}')
            data = kwargs.get('data') or {}
            files = kwargs.get('files') or {}
            self.upload_data = data
            self.upload_files = files
            return FakeResponse(json_data={
                'code': 0,
                'data': {
                    'workout_id': 88,
                },
            })
        raise AssertionError(f'unexpected POST {url}')


class ImxingzheApiTests(unittest.TestCase):
    def test_uses_current_token_api_and_fit_upload_fields(self):
        from imxingzhe import imxingzhe

        session = FakeXingzheSession()
        with patch('imxingzhe.requests.session', return_value=session), \
             patch.dict(os.environ, {
                 'IMXINGZHE_USERNAME': '13800138000',
                 'IMXINGZHE_PASSWORD': 'plain-password',
                 'EXPORT_PLATFORM': 'igpsport',
             }, clear=False):
            client = imxingzhe()
            client.login()
            client.import_fit(b'FIT-DATA', 'Morning Ride')

        self.assertEqual('xz-token', client.token)
        self.assertIn('/api/v1/user/login/', session.requests[0][1])
        login_payload = session.requests[0][2]['json']
        self.assertEqual('13800138000', login_payload['account'])
        self.assertNotEqual('plain-password', login_payload['password'])

        self.assertIn('/api/v1/fit/upload/', session.requests[1][1])
        self.assertEqual('Morning Ride', session.upload_data['name'])
        self.assertEqual('3', str(session.upload_data['sport']))
        self.assertEqual('iGPSPORT', session.upload_data['file_source'])
        self.assertIn('md5', session.upload_data)
        fit_file = session.upload_files['fit_file']
        self.assertEqual('Morning Ride.fit', fit_file[0])
        self.assertEqual(b'FIT-DATA', fit_file[1])

class FakeCookieTokenXingzheSession:
    def __init__(self):
        self.verify = True
        self.trust_env = True
        self.proxies = {}
        self.cookies = {
            '_XingzheWeb_AssessToken': 'cookie-token&expires=soon',
        }
        self.requests = []

    def post(self, url, **kwargs):
        self.requests.append(('POST', url, kwargs))
        if url.endswith('/api/v1/user/login/'):
            return FakeResponse(json_data={
                'code': 0,
                'data': {
                    'user_id': 123,
                    'nickname': 'rider',
                },
            })
        if url.endswith('/api/v1/fit/upload/'):
            headers = kwargs.get('headers') or {}
            if headers.get('Authorization') != 'Bearer cookie-token':
                raise AssertionError(f"missing cookie bearer token: {headers.get('Authorization')}")
            return FakeResponse(json_data={
                'code': 0,
                'data': {
                    'workout_id': 89,
                },
            })
        raise AssertionError(f'unexpected POST {url}')


class ImxingzheCookieTokenTests(unittest.TestCase):
    def test_uses_assess_token_cookie_when_login_body_has_no_access_token(self):
        from imxingzhe import imxingzhe

        session = FakeCookieTokenXingzheSession()
        with patch('imxingzhe.requests.session', return_value=session), \
             patch.dict(os.environ, {
                 'IMXINGZHE_USERNAME': '13800138000',
                 'IMXINGZHE_PASSWORD': 'plain-password',
                 'EXPORT_PLATFORM': 'igpsport',
             }, clear=False):
            client = imxingzhe()
            client.login()
            client.import_fit(b'FIT-DATA', 'Cookie Token Ride')

        self.assertEqual('cookie-token', client.token)

class FakeSessionCookieOnlyXingzheSession:
    def __init__(self):
        self.verify = True
        self.trust_env = True
        self.proxies = {}
        self.cookies = {
            'sessionid': 'session-cookie',
        }
        self.requests = []
        self.upload_headers = None

    def post(self, url, **kwargs):
        self.requests.append(('POST', url, kwargs))
        if url.endswith('/api/v1/user/login/'):
            return FakeResponse(json_data={
                'code': 0,
                'data': {
                    'user_id': 123,
                    'nickname': 'rider',
                },
            })
        if url.endswith('/api/v1/fit/upload/'):
            self.upload_headers = kwargs.get('headers') or {}
            if 'Authorization' in self.upload_headers:
                raise AssertionError('Authorization header should be omitted when token is unavailable')
            return FakeResponse(json_data={
                'code': 0,
                'data': {
                    'workout_id': 90,
                },
            })
        raise AssertionError(f'unexpected POST {url}')


class ImxingzheSessionCookieTests(unittest.TestCase):
    def test_allows_successful_login_without_access_token_when_session_cookies_exist(self):
        from imxingzhe import imxingzhe

        session = FakeSessionCookieOnlyXingzheSession()
        with patch('imxingzhe.requests.session', return_value=session), \
             patch.dict(os.environ, {
                 'IMXINGZHE_USERNAME': '13800138000',
                 'IMXINGZHE_PASSWORD': 'plain-password',
                 'EXPORT_PLATFORM': 'igpsport',
             }, clear=False):
            client = imxingzhe()
            client.login()
            client.import_fit(b'FIT-DATA', 'Session Cookie Ride')

        self.assertIsNone(client.token)
        self.assertEqual(2, len(session.requests))
if __name__ == '__main__':
    unittest.main()
