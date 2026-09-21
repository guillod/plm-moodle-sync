"""Moodle browser login and server-bound session storage."""

import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..common.files import atomic_write, encoded_json
from ..common.login import BrowserLoginError, LoginSpec
from ..common.urls import normalize_server
from .errors import MoodleError
from .html import script_objects
from .settings import DEFAULT_MOODLE_SERVER

LOGIN_HINT = 'Run python -m plm_moodle_sync moodle-login with the same configuration and session options.'

DASHBOARD_CHECK = """(() => {
    return !!(window.M && M.cfg && M.cfg.sesskey &&
        document.querySelector('a[href*="/login/logout.php"]') &&
        !document.body.classList.contains('notloggedin') &&
        !document.body.classList.contains('guestuser'));
})()"""


def is_moodle_dashboard(url, server):
    actual, expected = urlsplit(url), urlsplit(server + '/my/')
    return (actual.scheme, actual.netloc, actual.path.rstrip('/')) == (
        expected.scheme, expected.netloc, expected.path.rstrip('/'))


def page_session_key(html, server):
    soup = BeautifulSoup(html, 'html.parser')
    classes = soup.body.get('class', []) if soup.body else []
    logged_in = any(
        urlsplit(urljoin(server + '/', link['href'])).path == urlsplit(server + '/login/logout.php').path
        and urlsplit(urljoin(server + '/', link['href'])).netloc == urlsplit(server).netloc
        for link in soup.select('a[href]')
    )
    if logged_in and not {'notloggedin', 'guestuser'} & set(classes):
        for cfg in script_objects(html, r'\bM\.cfg\s*=\s*'):
            key = cfg.get('sesskey')
            if isinstance(key, str) and re.fullmatch(r'[A-Za-z0-9]+', key):
                return key
    raise MoodleError('Moodle session is missing, expired, or a guest session. ' + LOGIN_HINT)


def validate_cookies(cookies):
    if (not isinstance(cookies, dict) or not cookies or not all(
            isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_\-]+', name)
            and isinstance(value, str) and value and not any(ord(c) < 32 or c == ';' for c in value)
            for name, value in cookies.items())
            or not any(name.startswith('MoodleSession') for name in cookies)):
        raise MoodleError('Invalid Moodle session cookies. ' + LOGIN_HINT)
    return cookies


def load_cookies(path, server):
    try:
        session = json.loads(Path(path).expanduser().read_text(encoding='utf-8'))
    except FileNotFoundError as error:
        raise MoodleError('No Moodle session at ' + str(path) + '. ' + LOGIN_HINT) from error
    except (ValueError, UnicodeError) as error:
        raise MoodleError('Invalid Moodle session file. ' + LOGIN_HINT) from error
    if not isinstance(session, dict) or session.get('server') != normalize_server(server):
        raise MoodleError('This Moodle session does not belong to the configured server. ' + LOGIN_HINT)
    return validate_cookies(session.get('cookies'))


def login_spec(server):
    return LoginSpec(
        title='Moodle sign-in', start_url=server + '/my/',
        cookie_target=server + '/my/', cookie_names=None,
        is_dashboard=lambda url: is_moodle_dashboard(url, server), dashboard_script=DASHBOARD_CHECK,
        has_session=lambda cookies: any(name.startswith('MoodleSession') for name in cookies),
    )


def browser_login(server):
    try:
        from ..common.browser import run_login
    except ImportError as error:
        raise MoodleError('Browser login requires PySide6. From the project directory, install it with: '
                          'python -m pip install ".[login]"') from error
    try:
        return run_login(login_spec(server))
    except BrowserLoginError as error:
        raise MoodleError(str(error)) from error


def login(cookie_file, *, server=DEFAULT_MOODLE_SERVER, timeout=30):
    from .client import MoodleClient

    server = normalize_server(server)
    cookies = browser_login(server)
    if cookies is None:
        raise MoodleError('Moodle login cancelled; the previous session was kept.')
    validate_cookies(cookies)
    with MoodleClient(server, cookies, timeout=timeout) as client:
        client.authenticate()
    atomic_write(Path(cookie_file).expanduser(), encoded_json({'server': server, 'cookies': cookies}), mode=0o600)
