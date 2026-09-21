"""Publish one PDF through Moodle's File form and private draft area.

Reuse python-moodle's form extraction; use the real form's draft, repository,
and context IDs for upload. Requests stay within the authenticated Moodle site.
"""

from hashlib import sha256
from io import BytesIO
from pathlib import PurePosixPath
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup
import requests

from .errors import MoodleError


def _json(response):
    try:
        result = response.json()
    except (ValueError, TypeError) as error:
        raise MoodleError('Moodle returned an invalid file-operation response.') from error
    if result is False or (isinstance(result, dict) and (result.get('error') or result.get('exception'))):
        raise MoodleError('Moodle rejected a file operation; check permissions and session.')
    return result


def _content_path(url):
    parts = urlsplit(url)
    return unquote(parse_qs(parts.query).get('file', [parts.path])[0])


class FilePublisher:
    def __init__(self, client):
        self.client = client

    def pdf_hash(self, cmid, filename):
        """Read the published main PDF, checking its actual filename and bytes."""
        client = self.client
        response = client._request('GET', '/mod/resource/view.php', params={'id': cmid, 'redirect': 1})
        if not response.content.startswith(b'%PDF-'):
            soup = BeautifulSoup(response.text, 'html.parser')
            candidates = []
            for element in soup.select('a[href], object[data], iframe[src], embed[src]'):
                url = urljoin(response.url, element.get('href') or element.get('data') or element.get('src'))
                path = _content_path(url)
                if (client._same_site(url) and '/mod_resource/content/' in path
                        and PurePosixPath(path).name == filename and url not in candidates):
                    candidates.append(url)
            if not candidates:
                raise MoodleError(f'Cannot locate the published PDF {filename!r} in Moodle resource {cmid}.')
            response = client._request('GET', candidates[0][len(client.server):])
        path = _content_path(response.url)
        if '/mod_resource/content/' not in path:
            raise MoodleError(f'Moodle resource {cmid} returned an unexpected download location.')
        actual_filename = PurePosixPath(path).name
        if actual_filename != filename:
            raise MoodleError(f'Moodle resource {cmid} returned filename {actual_filename!r}; expected {filename!r}. '
                              'Check target.filename and the existing Moodle file.')
        if not response.content.startswith(b'%PDF-'):
            raise MoodleError(f'Moodle resource {cmid} did not return valid PDF content for {filename!r}.')
        return sha256(response.content).hexdigest()

    def _draft(self, action, itemid, **kwargs):
        return _json(self.client._request('POST', '/repository/draftfiles_ajax.php', data={
            'action': action, 'itemid': itemid, 'sesskey': self.client.sesskey, **kwargs,
        }))

    def _draft_files(self, itemid):
        data = self._draft('list', itemid, filepath='/')
        if not isinstance(data, dict) or not isinstance(data.get('list'), (list, type(None), bool)):
            raise MoodleError('Moodle returned an unsupported draft file listing.')
        files = data.get('list') or []
        if not isinstance(files, list) or any(not isinstance(file, dict) for file in files):
            raise MoodleError('Moodle returned an unsupported draft file listing.')
        return files

    def publish(self, course_id, section_number, filename, content, *, cmid=None, name=None,
                visible=None, replace_pdf=True, before_submit=lambda: None):
        """Stage and save a File resource. Caller must verify publication afterwards.

        before_submit persists retry information before the only public write.
        A timeout is propagated; callers reconcile the resource before retrying.
        replace_pdf=False saves settings while retaining the form's existing PDF.
        """
        if visible is not None and type(visible) is not bool:
            raise MoodleError('Resource visibility must be true, false, or omitted.')
        if not replace_pdf and cmid is None:
            raise MoodleError('Creating a Moodle resource requires a PDF upload.')
        try:
            from py_moodle.module import _extract_modedit_form_data
        except ImportError as error:
            raise MoodleError(
                'Moodle upload requires python-moodle==1.0.1 in the active Python environment. '
                'From the project directory, install with: python -m pip install -e .'
            ) from error

        client = self.client
        form, options = client._resource_form(course_id, section_number=section_number, cmid=cmid)
        if form is None or not options:
            raise MoodleError('The Moodle File editing form or upload repository is unavailable.')
        for disabled in form.select('[disabled]'):
            disabled.decompose()
        fields = _extract_modedit_form_data(form)
        if visible is not None:
            visibility = form.select_one('select[name="visible"]')
            if visibility is None or visibility.select_one(f'option[value="{int(visible)}"]') is None:
                raise MoodleError('The requested Moodle visibility setting is unavailable in the File form.')
            fields['visible'] = str(int(visible))
        if replace_pdf:
            repos = options.get('filepicker', {}).get('repositories', {})
            uploads = [repo for repo in repos.values() if repo.get('type') == 'upload']
            context = options.get('context', {}).get('id')
            if len(uploads) != 1 or not str(uploads[0].get('id', '')).isdigit() or not str(context).isdigit():
                raise MoodleError('Cannot determine the Moodle upload repository and context from the File form.')
            maximum = options.get('maxbytes')
            if isinstance(maximum, int) and maximum > 0 and len(content) > maximum:
                raise MoodleError(f'{filename} exceeds Moodle’s upload size limit.')
            if not content.startswith(b'%PDF-'):
                raise MoodleError('Only PDF content can be published.')
            itemid = fields['files']
            existing = self._draft_files(itemid)
            # Refuse to drop additional files someone added manually to a resource.
            if existing and (not cmid or len(existing) != 1 or existing[0].get('filename') != filename
                             or existing[0].get('filepath') != '/' or existing[0].get('type') == 'folder'):
                raise MoodleError('The File resource contains unexpected files; review it in Moodle before syncing.')
            for file in existing:
                self._draft('delete', itemid, filename=file['filename'], filepath='/')
            with BytesIO(content) as stream:
                result = _json(client._request('POST', '/repository/repository_ajax.php', params={'action': 'upload'}, data={
                    'sesskey': client.sesskey, 'repo_id': uploads[0]['id'], 'ctx_id': context,
                    'itemid': itemid, 'savepath': '/', 'title': filename, 'env': 'filemanager',
                    'accepted_types[]': '.pdf',
                }, files={'repo_upload_file': (filename, stream, 'application/pdf')}))
            if (not isinstance(result, dict) or result.get('event') or str(result.get('id')) != str(itemid)
                    or result.get('filename', result.get('file')) != filename):
                raise MoodleError('Moodle did not accept the PDF under its exact requested filename.')
            files = self._draft_files(itemid)
            if len(files) != 1 or files[0].get('filename') != filename or files[0].get('filepath') != '/':
                raise MoodleError('Moodle draft verification failed; the resource has not been saved.')
            self._draft('setmainfile', itemid, filename=filename, filepath='/')
        fields.update({'name': name if name is not None else filename,
                       'sesskey': client.sesskey, 'submitbutton2': 'Save and return to course'})
        # Editing an existing resource preserves its description, display,
        # availability and completion settings from the server form. Visibility
        # is preserved unless explicitly set by the configuration.
        fields.pop('coursecontentnotification', None)
        before_submit()
        try:
            response = client.session.request('POST', client.server + '/course/modedit.php', data=fields,
                                              timeout=client.timeout, allow_redirects=False)
        except requests.RequestException as error:
            raise MoodleError('Moodle save response was lost. Rerun upload to reconcile the resource before retrying.') from error
        if response.status_code not in (302, 303):
            raise MoodleError('Moodle did not confirm the File form save. Review required fields or permissions; rerun upload to reconcile.')
        destination = urljoin(client.server + '/course/modedit.php', response.headers.get('Location', ''))
        parts = urlsplit(destination)
        expected_path = urlsplit(client.server + '/course/view.php').path
        if (not client._same_site(destination) or parts.path != expected_path
                or parse_qs(parts.query).get('id') != [str(course_id)]):
            raise MoodleError('Moodle returned an unexpected save destination. Rerun upload to reconcile the resource.')
