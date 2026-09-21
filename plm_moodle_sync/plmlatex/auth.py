"""PLMlatex JSON sessions and institutional login.

The browser flow is adapted from overleaf-sync-plm. Copyright (c) 2021
Moritz Glöckl. See ../LICENSE.overleaf-sync and ../UPSTREAM.md.
"""

import json
from pathlib import Path
from urllib.parse import urlsplit

from ..common.files import atomic_write, encoded_json
from ..common.login import BrowserLoginError, LoginSpec
from ..common.urls import normalize_server
from .client import PLMlatexClient
from .errors import AuthenticationError
from .settings import DEFAULT_SERVER


LOGIN_HINT = 'Run python -m plm_moodle_sync plmlatex-login with the same configuration and session options.'
COOKIE_NAMES = {'oauth.session', 'sharelatex.sid', 'overleaf_session2'}

DASHBOARD_CHECK = """(() => {
    const meta = document.querySelector('meta[name="ol-projects"]');
    if (!meta) return false;
    try { return Array.isArray(JSON.parse(meta.content)); }
    catch (_) { return false; }
})()"""


def validate_cookies(cookies):
    if not isinstance(cookies, dict) or not cookies or not all(
        isinstance(k, str) and k and isinstance(v, str) and v
        and not any(char in k + v for char in '\r\n;')
        for k, v in cookies.items()
    ):
        raise AuthenticationError('The PLMlatex session must contain a dictionary of login cookies. ' + LOGIN_HINT)
    return cookies


def load_session(path, *, server=None):
    """Read server-bound JSON from a saved browser session."""
    path = Path(path).expanduser()
    try:
        session = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError as error:
        raise AuthenticationError('No PLMlatex session at ' + str(path) + '. ' + LOGIN_HINT) from error
    except (ValueError, UnicodeError):
        raise AuthenticationError('Invalid PLMlatex JSON session file. ' + LOGIN_HINT) from None
    if not isinstance(session, dict):
        raise AuthenticationError('Invalid PLMlatex session structure. ' + LOGIN_HINT)
    stored_server = session.get('server')
    if not isinstance(stored_server, str):
        raise AuthenticationError('Invalid PLMlatex session server. ' + LOGIN_HINT)
    try:
        stored_server = normalize_server(stored_server)
    except ValueError as error:
        raise AuthenticationError('Invalid PLMlatex session server. ' + LOGIN_HINT) from error
    if server is not None and stored_server != normalize_server(server):
        raise AuthenticationError('This PLMlatex session belongs to a different server. ' + LOGIN_HINT)
    return validate_cookies(session.get('cookies'))


def save_session(path, cookies, *, server=DEFAULT_SERVER):
    """Atomically store cookies as JSON, using the same structure as Moodle."""
    session = {'server': normalize_server(server), 'cookies': validate_cookies(cookies)}
    atomic_write(Path(path).expanduser(), encoded_json(session), mode=0o600)


def is_dashboard(url, server):
    """Only accept the configured server's project dashboard as login success."""
    target, expected = urlsplit(url), urlsplit(server + '/project')
    return ((target.scheme, target.netloc, target.path.rstrip('/')) ==
            (expected.scheme, expected.netloc, expected.path))


def login_spec(server):
    return LoginSpec(
        title='PLMlatex sign-in', start_url=server + '/login',
        cookie_target=server + '/project', cookie_names=COOKIE_NAMES,
        is_dashboard=lambda url: is_dashboard(url, server), dashboard_script=DASHBOARD_CHECK,
        has_session=lambda cookies: any(name in cookies for name in ('sharelatex.sid', 'overleaf_session2')),
    )


def browser_login(server):
    """Load optional GUI dependencies only when interactive login is requested."""
    try:
        from ..common.browser import run_login
    except ImportError as error:
        raise AuthenticationError('Browser login requires PySide6. From the project directory, install it with: '
                                  'python -m pip install ".[login]"') from error
    try:
        return run_login(login_spec(server))
    except BrowserLoginError as error:
        raise AuthenticationError(str(error)) from error


def login(cookie_file, *, server=DEFAULT_SERVER, timeout=30):
    """Validate a new browser session before atomically replacing the old file."""
    server = normalize_server(server)
    cookies = browser_login(server)
    if cookies is None:
        raise AuthenticationError('Login cancelled; the previous session was kept.')
    cookies = validate_cookies(cookies)
    # Checking the dashboard also works for an account with no projects.
    PLMlatexClient(cookie=cookies, base_url=server).all_projects(timeout=timeout)
    save_session(cookie_file, cookies, server=server)
