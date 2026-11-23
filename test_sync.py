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

    def mount(self, prefix, adapter):
        # 实现里用 session.mount 挂重试适配器；测试替身只需接受该调用。
        pass

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

            def get_activity_list(self):
                return []

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

    def test_skips_existing_activity_by_date_and_distance(self):
        import main

        class ExportPlatform:
            def login(self): pass
            def get_activity_list(self):
                return [{'ride_id': 11, 'title': '2023-06-01 户外骑行',
                         'origin_activity': {'id': 11, 'startTime': '2023.06.01',
                                             'rideDistance': 31450}}]
            def export_fit(self, activity):
                raise AssertionError('duplicate must not be downloaded')

        class ImportPlatform:
            def login(self): pass
            def get_activity_list(self):
                return [{'ride_id': 99, 'title': '2023-06-01 下午 骑行',
                         'origin_activity': {'rideId': 99, 'startTime': '2023.06.01',
                                             'rideDistance': 31450}}]
            def import_fit(self, fit_file, title):
                raise AssertionError('duplicate must not be uploaded')

        with patch.object(main, 'get_last_sync_id', return_value=0), \
             patch.object(main, 'set_last_sync_id') as set_last_sync_id:
            count, titles = main.sync_activities('igpsport', ExportPlatform(), ImportPlatform())

        self.assertEqual((0, []), (count, titles))
        set_last_sync_id.assert_called_once_with('igpsport', 11)

    def test_skips_same_activity_with_cross_platform_distance_difference(self):
        import main

        class ExportPlatform:
            def login(self): pass
            def get_activity_list(self):
                return [{'ride_id': 12, 'title': '2022-09-13 户外骑行',
                         'origin_activity': {'id': 12, 'startTime': '2022.09.13',
                                             'rideDistance': 13974}}]
            def export_fit(self, activity):
                raise AssertionError('same ride must not be downloaded')

        class ImportPlatform:
            def login(self): pass
            def get_activity_list(self):
                return [{'ride_id': 13, 'title': '2022-09-13 Ride activity',
                         'origin_activity': {'rideId': 13, 'startTime': '2022.09.13',
                                             'rideDistance': 12700}}]

        with patch.object(main, 'get_last_sync_id', return_value=0), \
             patch.object(main, 'set_last_sync_id') as set_last_sync_id:
            count, titles = main.sync_activities('igpsport', ExportPlatform(), ImportPlatform())

        self.assertEqual((0, []), (count, titles))
        set_last_sync_id.assert_called_once_with('igpsport', 12)

    def test_platform_list_includes_igpsport_xingzhe_and_intervalsicu(self):
        import main

        self.assertEqual({'igpsport', 'imxingzhe', 'intervalsicu'}, set(main.build_platform_list()))

    def test_parse_import_platforms_supports_comma_separated(self):
        import main

        with patch.dict(os.environ, {'IMPORT_PLATFORM': 'imxingzhe,intervalsicu'}, clear=False):
            self.assertEqual(['imxingzhe', 'intervalsicu'], main._parse_import_platforms())
        with patch.dict(os.environ, {'IMPORT_PLATFORM': '  intervalsicu , imxingzhe '}, clear=False):
            self.assertEqual(['intervalsicu', 'imxingzhe'], main._parse_import_platforms())
        with patch.dict(os.environ, {'IMPORT_PLATFORM': ''}, clear=False):
            self.assertEqual([], main._parse_import_platforms())

    def test_dual_write_uploads_same_activity_to_both_targets(self):
        """双写：同一条活动应分别上传到两个目标。"""
        import main

        class ExportPlatform:
            def login(self): pass
            def get_activity_list(self):
                return [{'ride_id': 20, 'title': '2026-09-20 骑行',
                         'origin_activity': {'id': 20, 'startTime': '2026.09.20',
                                             'rideDistance': 40000}}]
            def export_fit(self, activity):
                return b'FIT-DATA'

        def make_target():
            class Target:
                def __init__(self): self.imports = []
                def login(self): pass
                def get_activity_list(self): return []
                def import_file(self, data, title, ext):
                    self.imports.append((data, title, ext))
            return Target()

        a, b = make_target(), make_target()
        with patch.object(main, 'get_last_sync_id', return_value=0), \
             patch.object(main, 'set_last_sync_id') as set_last_sync_id:
            count, titles = main.sync_activities(
                'igpsport', ExportPlatform(), [('imxingzhe', a), ('intervalsicu', b)])

        self.assertEqual((1, ['2026-09-20 骑行']), (count, titles))
        self.assertEqual([(b'FIT-DATA', '2026-09-20 骑行', '.fit')], a.imports)
        self.assertEqual([(b'FIT-DATA', '2026-09-20 骑行', '.fit')], b.imports)
        set_last_sync_id.assert_called_once_with('igpsport', 20)

    def test_dual_write_skips_only_the_target_that_already_has_it(self):
        """其中一个目标已有该活动时，只跳过那一边，另一边照常上传。"""
        import main

        existing = {'ride_id': 99, 'title': '2026-09-20 骑行',
                    'origin_activity': {'id': 99, 'startTime': '2026.09.20',
                                        'rideDistance': 40000}}

        class ExportPlatform:
            def login(self): pass
            def get_activity_list(self):
                return [{'ride_id': 21, 'title': '2026-09-20 骑行',
                         'origin_activity': {'id': 21, 'startTime': '2026.09.20',
                                             'rideDistance': 40000}}]
            def export_fit(self, activity):
                return b'FIT-DATA'

        class HasIt:
            def login(self): pass
            def get_activity_list(self): return [existing]
            def import_file(self, data, title, ext):
                raise AssertionError('该目标已有此活动，不应重复上传')

        class NeedsIt:
            def __init__(self): self.imports = []
            def login(self): pass
            def get_activity_list(self): return []
            def import_file(self, data, title, ext):
                self.imports.append((data, title, ext))

        needs = NeedsIt()
        with patch.object(main, 'get_last_sync_id', return_value=0), \
             patch.object(main, 'set_last_sync_id'):
            count, titles = main.sync_activities(
                'igpsport', ExportPlatform(), [('imxingzhe', HasIt()), ('intervalsicu', needs)])

        self.assertEqual(1, count)
        self.assertEqual([(b'FIT-DATA', '2026-09-20 骑行', '.fit')], needs.imports)

    def test_dual_write_failure_does_not_advance_cursor(self):
        """关键回归：任一目标失败时不推进游标，否则失败那条永远不会补传。"""
        import main

        class ExportPlatform:
            def login(self): pass
            def get_activity_list(self):
                return [{'ride_id': 22, 'title': '2026-09-20 骑行',
                         'origin_activity': {'id': 22, 'startTime': '2026.09.20',
                                             'rideDistance': 40000}}]
            def export_fit(self, activity):
                return b'FIT-DATA'

        class Ok:
            def __init__(self): self.imports = []
            def login(self): pass
            def get_activity_list(self): return []
            def import_file(self, data, title, ext):
                self.imports.append(title)

        class Boom:
            def login(self): pass
            def get_activity_list(self): return []
            def import_file(self, data, title, ext):
                raise Exception('上传失败')

        ok = Ok()
        with patch.object(main, 'get_last_sync_id', return_value=0), \
             patch.object(main, 'set_last_sync_id') as set_last_sync_id:
            with self.assertRaises(Exception):
                main.sync_activities(
                    'igpsport', ExportPlatform(), [('imxingzhe', ok), ('intervalsicu', Boom())])

        self.assertEqual(['2026-09-20 骑行'], ok.imports)  # 先成功的那边已经上传
        set_last_sync_id.assert_not_called()               # 但游标绝不影响

    def test_check_differences_reports_each_target(self):
        import main

        class Source:
            def get_activity_list(self):
                return [{'ride_id': 1, 'title': '2026-09-20 骑行',
                         'origin_activity': {'id': 1, 'startTime': '2026.09.20',
                                             'rideDistance': 40000}}]

        class Target:
            def __init__(self, rows): self.rows = rows
            def get_activity_list(self): return self.rows

        empty = Target([])
        with patch.object(main, 'get_last_sync_id', return_value=0):
            results = main.check_differences(Source(), [('imxingzhe', empty), ('intervalsicu', empty)])

        self.assertEqual(2, len(results))
        self.assertEqual(['2026-09-20 骑行'], results[0][0])
        self.assertEqual(['2026-09-20 骑行'], results[1][0])

    def test_main_rejects_duplicate_targets(self):
        import main

        with patch.dict(os.environ, {'EXPORT_PLATFORM': 'igpsport',
                                     'IMPORT_PLATFORM': 'intervalsicu,intervalsicu'}, clear=False):
            with self.assertRaises(Exception) as ctx:
                main.main(run_once=True)
        self.assertIn('重复', str(ctx.exception))

    def test_main_rejects_unknown_target(self):
        import main

        with patch.dict(os.environ, {'EXPORT_PLATFORM': 'igpsport',
                                     'IMPORT_PLATFORM': 'igpsport,nope'}, clear=False):
            with self.assertRaises(Exception) as ctx:
                main.main(run_once=True)
        self.assertIn('导入平台配置不正确', str(ctx.exception))

    def test_main_rejects_bad_file_format_before_syncing(self):
        import main

        class P:
            def __init__(self): self.imports = []
            def login(self): pass
            def get_activity_list(self): return []
            def import_file(self, data, title, ext): self.imports.append(title)

        platforms = {'igpsport': P(), 'intervalsicu': P()}
        with patch.dict(os.environ, {'EXPORT_PLATFORM': 'igpsport',
                                     'IMPORT_PLATFORM': 'intervalsicu',
                                     'SYNC_FILE_FORMAT': 'docx'}, clear=False), \
             patch.object(main, 'build_platform_list', return_value=platforms):
            with self.assertRaises(Exception) as ctx:
                main.main(run_once=True)
        self.assertIn('SYNC_FILE_FORMAT', str(ctx.exception))

    def test_file_format_resolution_for_multi_target(self):
        import main

        with patch.dict(os.environ, {'SYNC_FILE_FORMAT': ''}, clear=False):
            self.assertEqual('fit', main._resolve_file_format('igpsport', 'intervalsicu'))
            self.assertEqual('fit', main._resolve_file_format('igpsport', 'imxingzhe'))
            self.assertEqual('gpx', main._resolve_file_format('imxingzhe', 'igpsport'))
        with patch.dict(os.environ, {'SYNC_FILE_FORMAT': 'tcx'}, clear=False):
            self.assertEqual('tcx', main._resolve_file_format('igpsport', 'intervalsicu'))


class FakeXingzheSession:
    def __init__(self):
        self.verify = True
        self.trust_env = True
        self.proxies = {}
        self.headers = {}
        self.requests = []

    def mount(self, prefix, adapter):
        pass

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

    def test_rejects_removed_upload_source(self):
        from imxingzhe import imxingzhe

        class NoUploadSession:
            def post(self, url, **kwargs):
                raise AssertionError('upload should not be attempted for removed source')

        with patch.dict(os.environ, {'EXPORT_PLATFORM': 'legacy'}, clear=False):
            client = imxingzhe()
            client.logged_in = True
            client.session = NoUploadSession()

            with self.assertRaisesRegex(Exception, '暂不支持导入该平台数据'):
                client.import_fit(b'FIT-DATA', 'Removed Source Ride')

class FakeCookieTokenXingzheSession:
    def __init__(self):
        self.verify = True
        self.trust_env = True
        self.proxies = {}
        self.cookies = {
            '_XingzheWeb_AssessToken': 'cookie-token&expires=soon',
        }
        self.requests = []

    def mount(self, prefix, adapter):
        pass

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

    def mount(self, prefix, adapter):
        pass

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
class FakeIcuSession:
    """intervals.icu 平台的测试替身：记录请求并回放固定响应。"""

    def __init__(self, athlete_status=200, list_status=200, upload_status=200):
        self.verify = True
        self.trust_env = True
        self.proxies = {}
        self.auth = None
        # 实现会把 User-Agent / Accept 设在 session.headers 上；替身需同样支持。
        self.headers = {}
        self.requests = []
        self.athlete_status = athlete_status
        self.list_status = list_status
        self.upload_status = upload_status

    def mount(self, prefix, adapter):
        pass

    def get(self, url, **kwargs):
        self.requests.append(('GET', url, kwargs))
        if url.endswith('/api/v1/athlete/0'):
            if self.athlete_status == 401:
                return FakeResponse(status_code=401, json_data={'message': 'unauthorized'})
            return FakeResponse(json_data={'id': 'i0', 'name': 'rider'})
        if url.endswith('/api/v1/athlete/0/activities'):
            if self.list_status >= 400:
                return FakeResponse(status_code=self.list_status, json_data={'message': 'boom'})
            # 接口按时间倒序返回
            return FakeResponse(json_data=[
                {'id': 'i222', 'name': 'Later Ride', 'start_date_local': '2026-09-20T07:30:00',
                 'distance': 31450.0, 'source': 'IGPSPORT'},
                {'id': 'i111', 'name': '', 'start_date_local': '2026-09-19T06:00:00',
                 'distance': 12700.0, 'source': 'STRAVA', 'strava_id': '999'},
            ])
        raise AssertionError(f'unexpected GET {url}')

    def post(self, url, **kwargs):
        self.requests.append(('POST', url, kwargs))
        if self.upload_status >= 400:
            return FakeResponse(status_code=self.upload_status,
                                json_data={'message': 'upload rejected'})
        return FakeResponse(json_data={'id': 'i333'})


class IntervalsIcuTests(unittest.TestCase):
    def _client(self, session, **env):
        from intervalsicu import intervalsicu

        base = {'INTERVALS_ICU_API_KEY': 'test-key'}
        base.update(env)
        with patch('intervalsicu.requests.session', return_value=session), \
             patch.dict(os.environ, base, clear=False):
            return intervalsicu()

    def test_login_fails_without_api_key(self):
        session = FakeIcuSession()
        with patch('intervalsicu.requests.session', return_value=session), \
             patch.dict(os.environ, {'INTERVALS_ICU_API_KEY': ''}, clear=False):
            from intervalsicu import intervalsicu
            client = intervalsicu()
            with self.assertRaises(Exception) as ctx:
                client.login()
        self.assertIn('INTERVALS_ICU_API_KEY', str(ctx.exception))

    def test_login_rejects_bad_key_with_401(self):
        session = FakeIcuSession(athlete_status=401)
        client = self._client(session)
        with self.assertRaises(Exception) as ctx:
            client.login()
        self.assertIn('鉴权失败', str(ctx.exception))
        self.assertFalse(client.logged_in)

    def test_login_uses_basic_auth_with_api_key_username(self):
        session = FakeIcuSession()
        client = self._client(session)
        client.login()
        self.assertTrue(client.logged_in)
        self.assertEqual(('API_KEY', 'test-key'), session.auth)

    def test_get_activity_list_returns_ascending_normalized_rows(self):
        session = FakeIcuSession()
        client = self._client(session)
        client.login()
        rows = client.get_activity_list()
        # 倒序输入 -> 升序输出，游标语义与 igpsport 一致
        self.assertEqual([111, 222], [r['ride_id'] for r in rows])
        self.assertEqual('Later Ride', rows[1]['title'])
        # 无标题时用日期兜底
        self.assertEqual('2026-09-19 骑行', rows[0]['title'])
        # 时间格式对齐 main.py 的 _activity_time
        self.assertEqual('2026-09-19 06:00:00', rows[0]['origin_activity']['start_time'])
        self.assertEqual(31450.0, rows[1]['origin_activity']['distance'])

    def test_get_activity_list_requires_login(self):
        client = self._client(FakeIcuSession())
        with self.assertRaises(Exception) as ctx:
            client.get_activity_list()
        self.assertIn('未登录', str(ctx.exception))

    def test_import_file_posts_multipart_with_name_param(self):
        session = FakeIcuSession()
        client = self._client(session)
        client.login()
        client.import_file(b'FIT-DATA', 'Morning Ride', '.fit')
        method, url, kwargs = session.requests[-1]
        self.assertEqual('POST', method)
        self.assertTrue(url.endswith('/api/v1/athlete/0/activities'))
        self.assertEqual('Morning Ride', kwargs['params']['name'])
        filename, payload, _ = kwargs['files']['file']
        self.assertEqual('Morning Ride.fit', filename)
        self.assertEqual(b'FIT-DATA', payload)

    def test_import_file_rejects_unsupported_extension(self):
        client = self._client(FakeIcuSession())
        client.login()
        with self.assertRaises(Exception) as ctx:
            client.import_file(b'DATA', 'Ride', '.csv')
        self.assertIn('不支持的文件格式', str(ctx.exception))

    def test_import_file_raises_on_upload_failure(self):
        session = FakeIcuSession(upload_status=500)
        client = self._client(session)
        client.login()
        with self.assertRaises(Exception) as ctx:
            client.import_file(b'DATA', 'Ride', '.fit')
        self.assertIn('上传失败', str(ctx.exception))

    def test_athlete_id_override_from_env(self):
        client = self._client(FakeIcuSession(), INTERVALS_ICU_ATHLETE_ID='i297087')
        self.assertEqual('https://intervals.icu/api/v1/athlete/i297087/activities',
                         client._athlete_url('/activities'))

    def test_numeric_id_strips_icu_prefix(self):
        from intervalsicu import intervalsicu

        self.assertEqual(12345678, intervalsicu._numeric_id('i12345678'))
        self.assertEqual(0, intervalsicu._numeric_id(None))
        self.assertEqual(0, intervalsicu._numeric_id(''))

    def test_parse_time_normalizes_iso_variants(self):
        from intervalsicu import intervalsicu

        self.assertEqual('2026-09-20 07:30:00', intervalsicu._parse_time('2026-09-20T07:30:00'))
        self.assertEqual('2026-09-20 07:30:00', intervalsicu._parse_time('2026-09-20T07:30:00.000+08:00'))
        self.assertEqual('2026-09-20 07:30:00', intervalsicu._parse_time('2026-09-20T07:30:00Z'))
        self.assertEqual('', intervalsicu._parse_time(''))


class IntervalsIcuDedupTests(unittest.TestCase):
    """ICU 读回的活动必须能被 main.py 的去重逻辑识别。"""

    def test_icu_row_is_detected_as_duplicate_by_main(self):
        import main
        from intervalsicu import intervalsicu

        # ICU 里的既有活动（已同步过）
        icu_row = intervalsicu._normalize({
            'id': 'i12345678', 'name': 'Morning Ride',
            'start_date_local': '2026-09-20T07:30:00', 'distance': 31450.0,
        })
        # iGPSPORT 里同一条骑行，日期+里程+开始时间都接近
        igp_row = {'ride_id': 42, 'title': '2026-09-20 上午骑行',
                   'origin_activity': {'rideId': 42, 'startTime': '2026.09.20 07:31:00',
                                       'rideDistance': 31400}}
        self.assertTrue(main.is_duplicate_activity(igp_row, [icu_row]))

    def test_icu_row_does_not_match_different_day(self):
        import main
        from intervalsicu import intervalsicu

        icu_row = intervalsicu._normalize({
            'id': 'i12345678', 'name': 'Morning Ride',
            'start_date_local': '2026-09-20T07:30:00', 'distance': 31450.0,
        })
        igp_row = {'ride_id': 43, 'title': '2026-09-21 上午骑行',
                   'origin_activity': {'rideId': 43, 'startTime': '2026.09.21 07:30:00',
                                       'rideDistance': 31400}}
        self.assertFalse(main.is_duplicate_activity(igp_row, [icu_row]))


if __name__ == '__main__':
    unittest.main()
