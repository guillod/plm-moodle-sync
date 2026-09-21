"""Optional real-Qt checks using synthetic HTML and cookies, without sign-in.

Run: QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -p browser_smoke.py -v
Requires the optional login dependencies.
"""

import time
import unittest

from PySide6.QtCore import QCoreApplication, QEvent, QUrl
from PySide6.QtNetwork import QNetworkCookie
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from plm_moodle_sync.common.browser import LoginWindow
from plm_moodle_sync.plmlatex.auth import login_spec as plmlatex_login_spec
from plm_moodle_sync.moodle.auth import login_spec as moodle_login_spec


class BrowserSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.window = LoginWindow(plmlatex_login_spec('https://latex.example/instance'))

    def tearDown(self):
        self.window.close()
        self.window.page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def cookie(self, domain='latex.example'):
        cookie = QNetworkCookie(b'sharelatex.sid', b'synthetic-test-cookie')
        cookie.setDomain(domain)
        cookie.setPath('/instance')
        self.window.cookie_added(cookie)

    def page(self, url, html):
        loaded = []
        self.window.webview.loadFinished.connect(lambda ok: loaded.append(ok))
        self.window.webview.setHtml(html, QUrl(url))
        self.wait_for(lambda: loaded)
        self.assertTrue(loaded[-1])

    def wait_for(self, condition):
        deadline = time.monotonic() + 10
        while not condition() and time.monotonic() < deadline:
            QTest.qWait(20)
        self.assertTrue(condition(), 'Timed out waiting for Qt WebEngine')

    def test_empty_dashboard_and_delayed_cookie_complete_login(self):
        self.page('https://latex.example/instance/project', '<meta name="ol-projects" content="[]">')
        self.wait_for(lambda: self.window.dashboard_ready)
        self.assertIsNone(self.window.result)
        self.cookie()
        self.assertEqual(self.window.result, {'sharelatex.sid': 'synthetic-test-cookie'})
        self.assertTrue(self.window.profile.isOffTheRecord())

    def test_wrong_origin_cannot_complete_login(self):
        self.cookie()
        self.page('https://sso.example/instance/project', '<meta name="ol-projects" content="[]">')
        self.assertIsNone(self.window.result)
        self.assertFalse(self.window.dashboard_ready)

    def test_login_page_and_cancellation_do_not_produce_a_session(self):
        self.cookie()
        self.page('https://latex.example/instance/project', '<h1>Please sign in</h1>')
        self.assertIsNone(self.window.result)
        self.window.close()
        self.window.dashboard_checked(True)
        self.assertIsNone(self.window.result)


class MoodleBrowserSmokeTests(BrowserSmokeTests):
    def setUp(self):
        self.window = LoginWindow(moodle_login_spec('https://moodle.example/instance'))

    def cookie(self, domain='moodle.example'):
        cookie = QNetworkCookie(b'MoodleSession', b'synthetic-test-cookie')
        cookie.setDomain(domain)
        cookie.setPath('/instance')
        self.window.cookie_added(cookie)

    def test_empty_dashboard_and_delayed_cookie_complete_login(self):
        self.page('https://moodle.example/instance/my/', '''
            <script>window.M = {cfg: {sesskey: "synthetic"}};</script>
            <a href="/instance/login/logout.php?sesskey=synthetic">Log out</a>
        ''')
        self.wait_for(lambda: self.window.dashboard_ready)
        self.assertIsNone(self.window.result)
        self.cookie('sso.example')
        self.assertIsNone(self.window.result)
        self.cookie()
        self.assertEqual(self.window.result, {'MoodleSession': 'synthetic-test-cookie'})
        self.assertTrue(self.window.profile.isOffTheRecord())

    def test_wrong_origin_cannot_complete_login(self):
        self.cookie()
        self.page('https://sso.example/instance/my/', '''
            <script>window.M = {cfg: {sesskey: "synthetic"}};</script>
            <a href="/instance/login/logout.php">Log out</a>
        ''')
        self.assertIsNone(self.window.result)

    def test_login_page_and_cancellation_do_not_produce_a_session(self):
        self.cookie()
        self.page('https://moodle.example/instance/my/', '''
            <script>window.M = {cfg: {sesskey: "synthetic"}};</script>
            <body class="notloggedin"><h1>Sign in</h1></body>
        ''')
        self.assertIsNone(self.window.result)
        self.window.close()
        self.window.dashboard_checked(True)
        self.assertIsNone(self.window.result)


if __name__ == '__main__':
    unittest.main()
