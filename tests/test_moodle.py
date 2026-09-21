"""Moodle session isolation, read-only checks, and section resolution."""

import contextlib
import io
import json
from http.cookies import SimpleCookie
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from plm_moodle_sync.cli import main
from plm_moodle_sync.common.login import CookieCapture
from plm_moodle_sync.config import ConfigError, load_config
from plm_moodle_sync.moodle.client import MoodleClient, resolve_section
from plm_moodle_sync.moodle.errors import MoodleError
from plm_moodle_sync.moodle.auth import is_moodle_dashboard, load_cookies, login, login_spec, page_session_key


SERVER = 'https://moodle.example/instance'
COOKIE = {'MoodleSession': 'syntheticCookie'}
DASHBOARD = '''<body><h1>Course test</h1>
    <script>M.cfg = {"sesskey":"syntheticKey"};</script>
    <a href="/instance/login/logout.php">Log out</a></body>'''
STATE = {'section': [{'id': 1234, 'number': 2, 'title': 'Travaux <em>dirigés</em>', 'cmlist': [5678]}],
         'cm': [{'id': 5678, 'name': 'TD1.pdf', 'module': 'resource'}]}
FORM = DASHBOARD + '''
    <form action="/instance/course/modedit.php" method="post">
    <input name="_qf__mod_resource_mod_form" value="1">
    <input name="course" value="301"><input name="coursemodule" value="5678">
    <input name="files" value="998877"></form>
    <script>M.form_filemanager.init(Y, {"itemid":998877,"maxbytes":1000000,
        "filepicker":{"repositories":{"4":{"id":4,"type":"upload","name":"Upload {file}"}}}});</script>
'''


def response(text='', data=None, status=200, location=None):
    return Mock(text=text, json=Mock(return_value=data), status_code=status,
                headers={'Location': location} if location else {})


class MoodleTests(unittest.TestCase):
    def setUp(self):
        self.client = MoodleClient(SERVER, COOKIE, timeout=7)
        self.addCleanup(self.client.session.close)

    def route(self, responses):
        pending = iter(responses)

        def handle(method, url, **kwargs):
            reply = next(pending)
            reply.url = url
            return reply

        request = Mock(side_effect=handle)
        self.client.session.request = request
        return request

    def test_auth_course_names_section_number_and_resource_forms_without_publication(self):
        request = self.route([response(DASHBOARD), response(DASHBOARD),
                              response(data=[{'data': json.dumps(STATE)}]), response(FORM), response(FORM)])
        report = self.client.check(301, section_name='Travaux dirigés')
        self.assertEqual(report['sections'][0]['name'], 'Travaux dirigés')
        self.assertEqual(report['sections'][0]['id'], 1234)
        self.assertEqual(report['create_file'], {'section_name': 'Travaux dirigés', 'accessible': True,
                                               'upload_repository': True, 'maxbytes': 1000000})
        self.assertEqual(report['update_file']['cmid'], 5678)
        self.assertFalse(report['publication_tested'])
        calls = request.call_args_list
        self.assertEqual(calls[3].kwargs['params']['section'], 2)  # Position, not database ID.
        for call in calls:
            self.assertFalse(call.kwargs['allow_redirects'])
            self.assertEqual(call.kwargs['timeout'], 7)
            if call.args[0] != 'GET':
                self.assertEqual(call.kwargs['json'][0]['methodname'], 'core_courseformat_get_state')
        self.assertNotIn('synthetic', json.dumps(report))

    def test_cross_origin_redirect_never_receives_cookies_or_session_key(self):
        request = self.route([response(status=302, location='https://sso.example/cas')])
        with self.assertRaisesRegex(MoodleError, 'redirected outside'):
            self.client.authenticate()
        self.assertEqual(request.call_count, 1)

    def test_resource_visibility_uses_selected_setting_from_current_form(self):
        for value in ('0', '1', '2'):
            with self.subTest(value=value):
                control = '<select name="visible">' + ''.join(
                    f'<option value="{option}"' + (' selected' if option == value else '') + '>Choice</option>'
                    for option in ('0', '1', '2')) + '</select>'
                request = self.route([response(FORM.replace('</form>', control + '</form>'))])
                self.assertEqual(self.client.resource_visibility(301, 5678), int(value))
                self.assertEqual(request.call_args.kwargs['params'], {'update': 5678})

    def test_resource_visibility_missing_or_unknown_setting_fails_explicitly(self):
        for control in ('', '<select name="visible"><option value="9" selected>Unknown</option></select>'):
            with self.subTest(control=control):
                self.route([response(FORM.replace('</form>', control + '</form>'))])
                with self.assertRaisesRegex(MoodleError, 'Cannot determine.*visibility'):
                    self.client.resource_visibility(301, 5678)

    def test_authenticated_redirect_to_another_landing_page_is_accepted(self):
        self.route([response(status=302, location=SERVER + '/?redirect=0'), response(DASHBOARD)])
        self.client.authenticate()
        self.assertEqual(self.client.sesskey, 'syntheticKey')

    def test_anonymous_or_guest_session_key_is_not_login_success(self):
        for html in (DASHBOARD.replace('<a ', '<span ').replace('</a>', '</span>'),
                     DASHBOARD.replace('<body>', '<body class="guestuser">'),
                     DASHBOARD.replace('<body>', '<body class="notloggedin">')):
            with self.subTest(html=html), self.assertRaises(MoodleError):
                page_session_key(html, SERVER)
        self.assertTrue(is_moodle_dashboard(SERVER + '/my/', SERVER))
        self.assertFalse(is_moodle_dashboard('https://sso.example/instance/my/', SERVER))

    def test_missing_and_duplicate_section_names_fail_before_form_requests(self):
        for state, name in [(STATE, 'Unknown'), ({**STATE, 'section': STATE['section'] * 2}, 'Travaux dirigés')]:
            request = self.route([response(DASHBOARD), response(DASHBOARD), response(data=[{'data': state}])])
            self.client.sesskey = None
            with self.subTest(name=name), self.assertRaises(MoodleError):
                self.client.check(301, section_name=name)
            self.assertEqual(request.call_count, 3)

    def test_section_ids_disambiguate_names_without_using_position(self):
        sections = [{'id': 1234, 'number': 2, 'name': 'Test'},
                    {'id': 5678, 'number': 3, 'name': 'Test'},
                    {'id': 9000, 'number': 4, 'name': '1234'}]
        self.assertEqual(resolve_section(sections, 1234), sections[0])
        self.assertEqual(resolve_section(sections, 5678), sections[1])
        self.assertEqual(resolve_section(sections, '1234'), sections[2])
        with self.assertRaisesRegex(MoodleError, 'ambiguous'):
            resolve_section(sections, 'Test')
        for selector in (2, 9999, '5678'):
            with self.subTest(selector=selector), self.assertRaisesRegex(MoodleError, 'not found in this course'):
                resolve_section(sections, selector)

    def test_section_selector_rejects_non_names_and_non_positive_ids(self):
        for selector in (True, False, 0, -1, 1234.0, None, [], '', '  '):
            with self.subTest(selector=selector), self.assertRaisesRegex(MoodleError, 'section selector'):
                resolve_section([], selector)

    def test_api_errors_do_not_echo_remote_messages_or_secrets(self):
        self.route([response(DASHBOARD), response(DASHBOARD), response(data=[{
            'error': True, 'exception': {'errorcode': 'nopermissions', 'message': 'privateSecret'},
        }])])
        with self.assertRaises(MoodleError) as error:
            self.client.check(301)
        self.assertIn('nopermissions', str(error.exception))
        self.assertNotIn('privateSecret', str(error.exception))

    def test_unavailable_form_is_reported_and_wrong_course_is_rejected(self):
        self.route([response(DASHBOARD)])
        self.assertFalse(self.client.resource_form(301, section_number=2)['accessible'])
        self.route([response(FORM.replace('value="301"', 'value="999"'))])
        with self.assertRaisesRegex(MoodleError, 'different course'):
            self.client.resource_form(301, section_number=2)


class MoodleSessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'moodle.json'
        self.old = json.dumps({'server': SERVER, 'cookies': COOKIE})
        self.path.write_text(self.old)

    def test_browser_session_is_validated_before_save_and_has_private_permissions(self):
        with patch('plm_moodle_sync.moodle.auth.browser_login', return_value={'MoodleSession': 'newCookie'}), \
             patch('plm_moodle_sync.moodle.client.MoodleClient.authenticate', side_effect=lambda: self.assertEqual(self.path.read_text(), self.old)):
            login(self.path, server=SERVER)
        self.assertEqual(load_cookies(self.path, SERVER), {'MoodleSession': 'newCookie'})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_cancelled_and_rejected_login_preserve_old_session(self):
        for result, error in [(None, None), (COOKIE, MoodleError('Expired'))]:
            with self.subTest(result=result), patch('plm_moodle_sync.moodle.auth.browser_login', return_value=result), \
                 patch('plm_moodle_sync.moodle.client.MoodleClient.authenticate', side_effect=error), self.assertRaises(MoodleError):
                login(self.path, server=SERVER)
            self.assertEqual(self.path.read_text(), self.old)

    def test_wrong_server_corrupt_or_unrelated_session_is_rejected(self):
        with self.assertRaises(MoodleError):
            load_cookies(self.path, 'https://other.example')
        for value in ('{}', '[]', '{bad', json.dumps({'server': SERVER, 'cookies': {'CAS': 'private'}})):
            self.path.write_text(value)
            with self.subTest(value=value), self.assertRaises(MoodleError):
                load_cookies(self.path, SERVER)

    def test_custom_session_name_and_site_affinity_cookie_are_retained_without_sso_cookies(self):
        capture = CookieCapture(SERVER + '/my/')
        capture.update('MoodleSessionTest', 'session', 'moodle.example', '/instance')
        capture.update('SERVERID', 'backend', 'moodle.example', '/')
        capture.update('SSO', 'private', 'sso.example', '/')
        cookies = capture.cookies()
        self.assertEqual(cookies, {'SERVERID': 'backend', 'MoodleSessionTest': 'session'})
        self.path.write_text(json.dumps({'server': SERVER, 'cookies': cookies}))
        self.assertEqual(load_cookies(self.path, SERVER), cookies)

    def test_cli_uses_global_course_and_config_relative_session(self):
        config_path = self.path.parent / 'sync.yaml'
        config_path.write_text('moodle:\n  server: ' + SERVER + '\n  cookie_file: moodle.json\n  course_id: 301\n')
        with patch('plm_moodle_sync.moodle.client.MoodleClient.check', return_value={'sections': []}) as check, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['moodle-check', '--config', str(config_path)]), 0)
        check.assert_called_once_with(301, section_name=None)
        config_path.write_text('moodle:\n  cookie_file: .cache/sync.json\n')
        with self.assertRaises(ConfigError):
            load_config(config_path)

    def test_browser_login_preserves_all_site_cookies_through_save_and_request_replay(self):
        # Preserve session, preference, and routing cookies through capture and replay.
        capture = login_spec(SERVER).capture()
        expected = {'MoodleSession': 'syntheticSession', 'MOODLEID1_': 'syntheticPreference',
                    'site-route': 'syntheticRoute'}
        for name, value in expected.items():
            capture.update(name, value, 'moodle.example', '/instance')
        capture.update('SSO', 'excluded', 'auth.example', '/')
        with patch('plm_moodle_sync.moodle.auth.browser_login', return_value=capture.cookies()), \
             patch('plm_moodle_sync.moodle.client.MoodleClient.authenticate'):
            login(self.path, server=SERVER)
        cookies = load_cookies(self.path, SERVER)
        self.assertEqual(cookies, expected)
        with MoodleClient(SERVER, cookies) as client:
            request = client.session.prepare_request(requests.Request('GET', SERVER + '/my/'))
            sent = SimpleCookie(request.headers['Cookie'])
            self.assertEqual({name: value.value for name, value in sent.items()}, expected)

    def test_network_errors_are_redacted_by_cli(self):
        with patch('plm_moodle_sync.moodle.client.MoodleClient.authenticate', side_effect=requests.Timeout('https://secret')), \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(['moodle-check', '--server', SERVER, '--cookie-file', str(self.path), '--course-id', '301']), 1)
        self.assertIn('Moodle network request failed', stderr.getvalue())
        self.assertNotIn('secret', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
