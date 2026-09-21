"""Authenticated Moodle requests, course structure, and File forms."""

import json
import re
import unicodedata
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
import requests

from ..common.urls import normalize_server
from .auth import LOGIN_HINT, page_session_key, validate_cookies
from .errors import MoodleError
from .html import script_objects, visible_text


def resolve_section(sections, selector):
    """Resolve a string name or an integer database ID within one course."""
    if type(selector) is int and selector > 0:
        matches = [section for section in sections if section['id'] == selector]
        label = f'Section ID {selector}'
    elif isinstance(selector, str) and selector.strip():
        expected = unicodedata.normalize('NFC', ' '.join(selector.split()))
        matches = [section for section in sections if section['name'] == expected]
        label = f'Section {selector!r}'
    else:
        raise MoodleError('A section selector must be a name string or a positive integer section ID.')
    if len(matches) != 1:
        reason = 'was not found in this course' if not matches else 'is ambiguous (several sections match)'
        raise MoodleError(f'{label} {reason}.')
    return matches[0]


class MoodleClient:
    def __init__(self, server, cookies, *, timeout=30):
        self.server = normalize_server(server)
        self.timeout = timeout
        self.session = requests.Session()
        for name, value in validate_cookies(cookies).items():
            self.session.cookies.set(name, value, domain=urlsplit(server).hostname,
                                     path=urlsplit(self.server).path + '/', secure=True)
        self.sesskey = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.session.close()

    def _same_site(self, url):
        actual, expected = urlsplit(url), urlsplit(self.server)
        return (actual.scheme, actual.netloc) == (expected.scheme, expected.netloc) and (
            actual.path == expected.path or actual.path.startswith(expected.path + '/'))

    def _request(self, method, path, **kwargs):
        url = self.server + path
        for _ in range(6):
            if not self._same_site(url):
                raise MoodleError('Moodle redirected outside the configured site. ' + LOGIN_HINT)
            response = self.session.request(method, url, timeout=self.timeout,
                                            allow_redirects=False, **kwargs)
            if response.status_code in (301, 302, 303, 307, 308):
                if method != 'GET':
                    raise MoodleError('Moodle redirected an API request. ' + LOGIN_HINT)
                url = urljoin(url, response.headers.get('Location', ''))
                kwargs = {}
                continue
            if response.status_code != 200:
                raise MoodleError(f'Moodle returned HTTP {response.status_code}.')
            return response
        raise MoodleError('Moodle redirected too many times. ' + LOGIN_HINT)

    def authenticate(self):
        response = self._request('GET', '/my/')
        # Moodle may redirect /my/ to a configured landing page. The same-site
        # response must contain both authenticated navigation and M.cfg.sesskey.
        try:
            self.sesskey = page_session_key(response.text, self.server)
        except MoodleError as error:
            soup = BeautifulSoup(response.text, 'html.parser')
            # Only structural diagnostics: never HTML, URLs with queries, or tokens.
            cfg_found = any(script_objects(response.text, r'\bM\.cfg\s*=\s*'))
            logout_found = bool(soup.select_one('a[href*="/login/logout.php"]'))
            raise MoodleError('Moodle session validation failed (landing path: '
                              + urlsplit(response.url).path + f'; configuration: {cfg_found}; logout link: {logout_found}). '
                              + LOGIN_HINT) from error

    def course(self, course_id):
        if type(course_id) is not int or course_id <= 0:
            raise MoodleError('course_id must be a positive integer.')
        if not self.sesskey:
            self.authenticate()
        page = self._request('GET', '/course/view.php', params={'id': course_id})
        self.sesskey = page_session_key(page.text, self.server)
        soup = BeautifulSoup(page.text, 'html.parser')
        title = soup.select_one('h1') or soup.title
        response = self._request('POST', '/lib/ajax/service.php', params={'sesskey': self.sesskey}, json=[{
            'index': 0, 'methodname': 'core_courseformat_get_state', 'args': {'courseid': course_id},
        }])
        try:
            envelope = response.json()
            if not isinstance(envelope, list) or len(envelope) != 1 or not isinstance(envelope[0], dict):
                raise ValueError()
            if envelope[0].get('error'):
                code = envelope[0].get('exception', {}).get('errorcode', '')
                code = code if isinstance(code, str) and re.fullmatch(r'[a-z0-9_]+', code) else 'unknown'
                raise MoodleError('Moodle course lookup failed (' + code + '). Check course access and session.')
            state = envelope[0]['data']
            if isinstance(state, str):
                state = json.loads(state)
            if not isinstance(state, dict) or not isinstance(state.get('section'), list):
                raise ValueError()
            sections = []
            modules = {str(module['id']): module for module in state.get('cm', [])}
            for raw in state['section']:
                number = int(raw.get('number', raw.get('section', -1)))
                name = visible_text(raw.get('title') or raw.get('name') or '')
                if not name or number < 0:
                    raise ValueError()
                members = []
                for identifier in raw.get('cmlist', []):
                    module = modules[str(identifier)]
                    members.append({'id': int(module['id']), 'name': visible_text(module.get('name', '')),
                                    'modname': module.get('module', module.get('modname', module.get('mod', 'unknown')))})
                sections.append({'id': int(raw['id']), 'number': number, 'name': name, 'modules': members})
        except (ValueError, TypeError, KeyError) as error:
            raise MoodleError('Moodle returned an unsupported course structure.') from error
        return {'course_id': course_id, 'name': visible_text(title.get_text()) if title else '', 'sections': sections}

    def _resource_form(self, course_id, *, section_number=None, cmid=None):
        params = {'update': cmid} if cmid else {'add': 'resource', 'course': course_id, 'section': section_number}
        response = self._request('GET', '/course/modedit.php', params=params)
        self.sesskey = page_session_key(response.text, self.server)
        soup = BeautifulSoup(response.text, 'html.parser')
        marker = soup.select_one('input[name="_qf__mod_resource_mod_form"]')
        form = marker.find_parent('form') if marker else None
        if form is None:
            return None, None
        action = urljoin(response.url, form.get('action', ''))
        if not self._same_site(action) or urlsplit(action).path != urlsplit(self.server + '/course/modedit.php').path:
            raise MoodleError('Unexpected Moodle File form destination.')
        fields = {field.get('name'): field.get('value') for field in form.select('input[name]')}
        if str(fields.get('course')) != str(course_id) or (cmid and str(fields.get('coursemodule')) != str(cmid)):
            raise MoodleError('Moodle returned a File form for a different course or resource.')
        draft = fields.get('files')
        if not draft or not str(draft).isdigit():
            return None, None
        for options in script_objects(response.text, r'\bM\.form_filemanager\.init\s*\(\s*(?:Y\s*,\s*)?'):
            if str(options.get('itemid')) != str(draft):
                continue
            return form, options
        return form, None

    def resource_form(self, course_id, *, section_number=None, cmid=None):
        form, options = self._resource_form(course_id, section_number=section_number, cmid=cmid)
        result = {'accessible': form is not None, 'upload_repository': False}
        if options:
            repos = options.get('filepicker', {}).get('repositories', {})
            result['upload_repository'] = any(repo.get('type') == 'upload' for repo in repos.values())
            result['maxbytes'] = options.get('maxbytes')
        return result

    def resource_visibility(self, course_id, cmid):
        """Read the resource's own visibility setting from the current edit form."""
        form, _ = self._resource_form(course_id, cmid=cmid)
        selected = form.select_one('select[name="visible"] option[selected]') if form is not None else None
        # Moodle may also offer 2: available by link, but not on the course page.
        if selected is None or selected.get('value') not in ('0', '1', '2'):
            raise MoodleError('Cannot determine the Moodle resource visibility from the File form.')
        return int(selected['value'])

    def check(self, course_id, *, section_name=None):
        report = self.course(course_id)
        sections = report['sections']
        selected = resolve_section(sections, section_name) if section_name else next(iter(sections), None)
        if selected:
            report['create_file'] = {'section_name': selected['name'], **self.resource_form(
                course_id, section_number=selected['number'])}
        resource = next((module for section in ([selected] if section_name else sections)
                         for module in section['modules'] if module['modname'] == 'resource'), None)
        report['update_file'] = ({'cmid': resource['id'], **self.resource_form(course_id, cmid=resource['id'])}
                                 if resource else {'checked': False, 'reason': 'No existing File resource in the selected scope.'})
        report['publication_tested'] = False
        return report
