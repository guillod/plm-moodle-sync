"""Optional Qt browser for institutional sign-in.

Adapted from overleaf-sync-plm's olbrowserlogin.py. Copyright (c) 2021
Moritz Glöckl. See ../LICENSE.overleaf-sync and ../UPSTREAM.md.
"""

from PySide6.QtCore import QCoreApplication, QEvent, QUrl
from PySide6.QtWidgets import QApplication, QMainWindow
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView

from .login import BrowserLoginError


class LoginWindow(QMainWindow):
    def __init__(self, spec):
        super().__init__()
        self.spec = spec
        self.capture = spec.capture()
        self.dashboard_ready = False
        self.closed = False
        self.result = None
        self.webview = QWebEngineView(self)
        # An unnamed profile keeps browsing data in memory only.
        self.profile = QWebEngineProfile(self)
        self.profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
        self.page = QWebEnginePage(self.profile, self.webview)
        self.webview.setPage(self.page)
        self.cookie_store = self.profile.cookieStore()
        self.cookie_store.cookieAdded.connect(self.cookie_added)
        self.cookie_store.cookieRemoved.connect(self.cookie_removed)
        self.webview.urlChanged.connect(self.url_changed)
        self.webview.loadFinished.connect(self.load_finished)
        self.setCentralWidget(self.webview)
        self.setWindowTitle(spec.title)
        self.resize(900, 750)

    def cookie_added(self, cookie):
        self.capture.update(bytes(cookie.name()).decode(), bytes(cookie.value()).decode(), cookie.domain(), cookie.path())
        self.maybe_finish()

    def cookie_removed(self, cookie):
        self.capture.update(bytes(cookie.name()).decode(), '', cookie.domain(), cookie.path(), removed=True)

    def url_changed(self, url):
        self.dashboard_ready = False

    def load_finished(self, ok):
        if not self.closed and ok and self.on_dashboard():
            self.page.runJavaScript(self.spec.dashboard_script, self.dashboard_checked)

    def on_dashboard(self):
        return self.spec.is_dashboard(self.webview.url().toString())

    def dashboard_checked(self, ready):
        if self.closed:
            return
        self.dashboard_ready = bool(ready) and self.on_dashboard()
        self.maybe_finish()

    def maybe_finish(self):
        cookies = self.capture.cookies()
        if not self.closed and self.dashboard_ready and self.spec.has_session(cookies):
            self.result = cookies
            self.close()

    def closeEvent(self, event):
        self.closed = True
        super().closeEvent(event)


def run_login(spec):
    if QApplication.instance() is not None:
        raise BrowserLoginError('Run browser login in a separate process using the login command for this service.')
    app = QApplication([])
    window = LoginWindow(spec)
    window.show()
    window.webview.load(QUrl(spec.start_url))
    app.exec()
    result = window.result
    # Destroy the page before its profile, including when login is cancelled.
    window.page.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    return result
