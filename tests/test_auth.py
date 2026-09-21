"""Authentication regression tests use synthetic cookies and mocked sign-in."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import requests

from plm_moodle_sync.plmlatex.auth import login_spec, is_dashboard, load_session, login, save_session
from plm_moodle_sync.cli import main
from plm_moodle_sync.plmlatex.errors import AuthenticationError
from plm_moodle_sync.common.files import atomic_write


class SessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.session = Path(temporary.name) / 'plmlatex.json'
        self.old = json.dumps({'server': 'https://latex.example', 'cookies': {'sharelatex.sid': 'old-cookie'}}).encode()
        self.session.write_bytes(self.old)

    def test_login_validates_then_saves_json_session_with_private_permissions(self):
        def validate(**kwargs):
            self.assertEqual(self.session.read_bytes(), self.old)
            return []  # Login does not require an existing project.

        with patch('plm_moodle_sync.plmlatex.auth.browser_login', return_value={'sharelatex.sid': 'new-cookie'}) as browser, \
             patch('plm_moodle_sync.plmlatex.auth.PLMlatexClient') as client:
            client.return_value.all_projects.side_effect = validate
            login(self.session, server='https://latex.example/instance/', timeout=7)
        browser.assert_called_once_with('https://latex.example/instance')
        client.assert_called_once_with(cookie={'sharelatex.sid': 'new-cookie'}, base_url='https://latex.example/instance')
        client.return_value.all_projects.assert_called_once_with(timeout=7)
        self.assertEqual(json.loads(self.session.read_text()), {
            'server': 'https://latex.example/instance', 'cookies': {'sharelatex.sid': 'new-cookie'},
        })
        self.assertEqual(load_session(self.session, server='https://latex.example/instance/'), {'sharelatex.sid': 'new-cookie'})
        if os.name == 'posix':
            self.assertEqual(self.session.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.session.parent.iterdir()), [self.session])

    def test_json_sessions_require_a_valid_matching_server_and_cookie_dictionary(self):
        valid = {'server': 'https://latex.example', 'cookies': {'sharelatex.sid': 'test'}}
        self.session.write_text(json.dumps(valid))
        self.assertEqual(load_session(self.session, server='https://latex.example/'), valid['cookies'])
        with self.assertRaisesRegex(AuthenticationError, 'different server'):
            load_session(self.session, server='https://another.example')
        for invalid in (
                [], None, {'cookies': valid['cookies']},
                *({**valid, 'server': server} for server in (None, 3, [], 'http://latex.example',
                                                           'https://user:secret@latex.example')),
                *({**valid, 'cookies': cookies} for cookies in (None, [], {}, {'sid': 'a\r\nb'})),
                {'server': valid['server'], 'cookie': valid['cookies']},
        ):
            with self.subTest(invalid=invalid):
                self.session.write_text(json.dumps(invalid))
                with self.assertRaises(AuthenticationError) as raised:
                    load_session(self.session, server='https://latex.example')
                self.assertNotIn('secret', str(raised.exception))

    def test_json_session_round_trip_preserves_all_cookies(self):
        cookies = {'sharelatex.sid': 'session-value', 'oauth.session': 'oauth-value'}
        save_session(self.session, cookies, server='https://latex.example/')
        self.assertEqual(load_session(self.session, server='https://latex.example'), cookies)
        self.assertEqual(json.loads(self.session.read_text()), {
            'server': 'https://latex.example', 'cookies': cookies,
        })

    def test_invalid_json_save_preserves_existing_session(self):
        with self.assertRaises(AuthenticationError):
            save_session(self.session, {}, server='https://latex.example')
        self.assertEqual(self.session.read_bytes(), self.old)

    def test_cancelled_or_rejected_login_preserves_existing_session(self):
        for cookies, error in [(None, None), ({}, None), ({'sharelatex.sid': 'new'}, AuthenticationError('Expired')),
                               ({'sharelatex.sid': 'new'}, requests.Timeout('network-secret'))]:
            with self.subTest(cookies=cookies, error=type(error).__name__), \
                 patch('plm_moodle_sync.plmlatex.auth.browser_login', return_value=cookies), \
                 patch('plm_moodle_sync.plmlatex.auth.PLMlatexClient') as client:
                client.return_value.all_projects.side_effect = error
                with self.assertRaises((AuthenticationError, requests.Timeout)):
                    login(self.session)
                self.assertEqual(self.session.read_bytes(), self.old)

    def test_failed_atomic_replacement_preserves_old_session_and_cleans_temporary(self):
        with patch.object(Path, 'replace', side_effect=OSError('disk failure')), self.assertRaises(OSError):
            atomic_write(self.session, b'new', mode=0o600)
        self.assertEqual(self.session.read_bytes(), self.old)
        self.assertEqual(list(self.session.parent.iterdir()), [self.session])

    def test_missing_or_invalid_session_explains_how_to_login(self):
        for content in (None, b'invalid-json', b'\x80\xff', b'[]', b'{"cookies": {}}'):
            with self.subTest(content=content):
                if content is None:
                    self.session.unlink()
                else:
                    self.session.write_bytes(content)
                with self.assertRaisesRegex(AuthenticationError, 'python -m plm_moodle_sync plmlatex-login'):
                    load_session(self.session)

    def test_invalid_server_never_opens_browser(self):
        with patch('plm_moodle_sync.plmlatex.auth.browser_login') as browser:
            for server in ('http://latex.example', 'https://user:secret@latex.example', 'https://latex.example/?token=secret'):
                with self.subTest(server=server), self.assertRaises(ValueError):
                    login(self.session, server=server)
            browser.assert_not_called()

    def test_login_cli_reports_network_failure_without_exposing_cookie_or_url(self):
        with patch('plm_moodle_sync.plmlatex.auth.browser_login', return_value={'sharelatex.sid': 'cookie-secret'}), \
             patch('plm_moodle_sync.plmlatex.auth.PLMlatexClient.all_projects', side_effect=requests.Timeout('https://host/?url-secret')), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(['plmlatex-login', '--cookie-file', str(self.session)]), 1)
        self.assertNotIn('secret', stderr.getvalue())
        self.assertEqual(self.session.read_bytes(), self.old)

    def test_login_cli_passes_configured_server_and_cookie_path(self):
        with patch('plm_moodle_sync.cli.login') as call, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['plmlatex-login', '--server', 'https://latex.example', '--cookie-file', str(self.session), '--timeout', '9']), 0)
        call.assert_called_once_with(self.session, server='https://latex.example', timeout=9)


class CookieCaptureTests(unittest.TestCase):
    def test_only_session_cookies_for_configured_host_and_path_are_captured(self):
        cookies = login_spec('https://latex.example/instance').capture()
        cookies.update('sharelatex.sid', 'correct', 'latex.example', '/instance')
        cookies.update('oauth.session', 'parent-domain', '.example', '/')
        cookies.update('oauth.session', 'sso', 'sso.example', '/')
        cookies.update('sharelatex.sid', 'suffix-confusion', 'notlatex.example', '/')
        cookies.update('sharelatex.sid', 'path-confusion', 'latex.example', '/inst')
        cookies.update('unrelated', 'irrelevant', 'latex.example', '/')
        self.assertEqual(cookies.cookies(), {'oauth.session': 'parent-domain', 'sharelatex.sid': 'correct'})

    def test_cookie_updates_removals_and_path_precedence(self):
        cookies = login_spec('https://latex.example/instance').capture()
        cookies.update('sharelatex.sid', 'root', 'latex.example', '/')
        cookies.update('sharelatex.sid', 'specific', 'latex.example', '/instance')
        cookies.update('sharelatex.sid', 'root-updated', 'latex.example', '/')
        self.assertEqual(cookies.cookies()['sharelatex.sid'], 'specific')
        cookies.update('sharelatex.sid', '', 'latex.example', '/instance', removed=True)
        self.assertEqual(cookies.cookies()['sharelatex.sid'], 'root-updated')

    def test_dashboard_requires_matching_origin_and_path(self):
        server = 'https://latex.example/instance'
        self.assertTrue(is_dashboard(server + '/project/', server))
        self.assertTrue(is_dashboard(server + '/project?order=title', server))
        for url in ('https://sso.example/instance/project', server + '/project/id',
                    'http://latex.example/instance/project', server + '/login'):
            self.assertFalse(is_dashboard(url, server))


class StandalonePackageTests(unittest.TestCase):
    def test_fetch_import_and_help_work_with_olsync_and_qt_blocked(self):
        code = '''
import importlib.abc
import sys
class BlockOptional(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'olsync', 'PySide6'}:
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, BlockOptional())
from plm_moodle_sync.plmlatex.auth import browser_login
from plm_moodle_sync.plmlatex.errors import AuthenticationError
try:
    browser_login('https://latex.example')
except AuthenticationError as error:
    assert '.[login]' in str(error)
else:
    raise AssertionError('Expected optional-dependency error')
from plm_moodle_sync.cli import main
main(['fetch', '--help'])
'''
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--tex', result.stdout)


if __name__ == '__main__':
    unittest.main()
