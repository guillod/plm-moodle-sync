"""PLMlatex retrieval client, derived from overleaf-sync-plm.

Original code: Copyright (c) 2021 Moritz Glöckl, distributed under MIT.
See ../LICENSE.overleaf-sync and ../UPSTREAM.md for attribution and licensing.
"""

from contextlib import contextmanager
import json
from pathlib import PurePosixPath
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
import requests as reqs
from socketIO_client import SocketIO
from socketIO_client.exceptions import SocketIOError

from .errors import AuthenticationError, CompilationError
from .settings import DEFAULT_SERVER
from ..common.urls import normalize_server


class ProjectSocket(SocketIO):
    """Avoid reconnecting in the legacy client's destructor after disconnect()."""

    def __del__(self):
        if self.connected:
            self.disconnect()


class PLMlatexClient:
    """Query project sources and compile selected documents on PLMlatex."""

    def __init__(self, cookie, base_url=DEFAULT_SERVER):
        self._cookie = cookie
        self._csrf = None
        self._base_url = normalize_server(base_url)

    @property
    def base_url(self):
        return self._base_url

    def all_projects(self, timeout=30):
        """List projects that are neither archived nor trashed."""
        projects_page = reqs.get(
            self._base_url + '/project', cookies=self._cookie,
            timeout=timeout, allow_redirects=False,
        )
        self._check_response(projects_page, 'Listing projects')
        metadata = BeautifulSoup(projects_page.content, 'html.parser').find('meta', {'name': 'ol-projects'})
        if metadata is None or not metadata.get('content'):
            raise AuthenticationError('No project list on the dashboard; renew the login session.')
        try:
            json_content = json.loads(metadata['content'])
        except ValueError as error:
            raise CompilationError('The dashboard returned an invalid project list.') from error
        if not isinstance(json_content, list) or any(not isinstance(project, dict) for project in json_content):
            raise CompilationError('The dashboard returned an unexpected project list.')
        return [project for project in json_content if not project.get('archived') and not project.get('trashed')]

    def get_project(self, project_name):
        """Find an active project by exact name, rejecting ambiguous matches."""
        matches = [project for project in self.all_projects() if project.get('name') == project_name]
        if len(matches) > 1:
            raise CompilationError('Several projects have that name; select one by project ID.')
        return matches[0] if matches else None

    def get_project_infos(self, project_id, timeout=30):
        """Read the project tree and compiler settings."""
        with self.project_connection(project_id, timeout) as (socket_io, project_infos):
            return project_infos

    @contextmanager
    def project_connection(self, project_id, timeout=30):
        """Read project metadata and optionally documents over one connection."""
        project_infos = None

        def set_project_infos(error, project_infos_dict=None, *unused):
            nonlocal project_infos
            if not error:
                project_infos = project_infos_dict

        # Build the Cookie header for the editor connection.
        cookie = "; ".join("{}={}".format(name, value) for name, value in self._cookie.items())
        socket_io = ProjectSocket(
            self._base_url,
            params={'t': int(time.time())},
            headers={'Cookie': cookie},
            wait_for_connection=False,
            timeout=timeout,
        )

        try:
            # Register the default namespace before receiving project events.
            socket_io.on('connect', lambda: None)
            socket_io.emit('joinProject', {'project_id': project_id}, set_project_infos)
            socket_io.wait_for_callbacks(seconds=timeout)
            if not isinstance(project_infos, dict):
                raise CompilationError("Could not retrieve the project tree; check access and renew the login session.")
            yield socket_io, project_infos
        except SocketIOError as error:
            raise CompilationError("Could not connect to the project metadata service; check connectivity and the login session.") from error
        finally:
            if socket_io.connected:
                socket_io.disconnect()

    @staticmethod
    def read_document(socket_io, doc_id, previous_version=-1, timeout=30):
        """Read a revision, fetching full source only if needed or requested.

        Servers may send full text even for an unchanged revision (e.g. after
        clearing their operation cache). The caller can still reuse its source.
        """
        def read(version):
            replies = []
            socket_io.emit('joinDoc', doc_id, version, {}, lambda *args: replies.append(args))
            socket_io.wait_for_callbacks(seconds=timeout)
            if not replies or len(replies[0]) < 3 or replies[0][0] is not None:
                raise CompilationError('Could not read the document revision.')
            _, lines, current_version, *unused = replies[0]
            if type(current_version) is not int:
                raise CompilationError('The document returned an invalid revision.')
            socket_io.emit('leaveDoc', doc_id)
            return lines, current_version

        lines, version = read(previous_version)
        if lines is None and version != previous_version:
            lines, version = read(-1)
        if lines is None:
            return {'version': version, 'text': None}
        if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
            raise CompilationError('The document returned invalid source text.')
        source = '\n'.join(lines)
        try:
            source = source.encode('latin1').decode('utf8')
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        return {'version': version, 'text': source}

    @staticmethod
    def project_entries(project_infos):
        """List editable documents and uploaded resources, with stable identities."""
        entries = {}

        def walk(folder, prefix=''):
            for kind, key in (('doc', 'docs'), ('file', 'fileRefs')):
                for item in folder.get(key, []):
                    path = prefix + item['name']
                    if path in entries:
                        raise CompilationError('Duplicate path in the project: ' + path)
                    entries[path] = {'kind': kind, 'id': str(item['_id'])}
                    for field in ('hash', 'rev'):
                        if item.get(field) is not None:
                            entries[path][field] = item[field]
            for child in folder.get('folders', []):
                walk(child, prefix + child['name'] + '/')

        roots = project_infos.get('rootFolder', [])
        for root in ([roots] if isinstance(roots, dict) else roots):
            walk(root)
        return entries

    def download_file(self, project_id, file_id, timeout=30):
        response = reqs.get(
            self._base_url + '/project/' + project_id + '/file/' + file_id,
            cookies=self._cookie, timeout=timeout, allow_redirects=False,
        )
        self._check_response(response, 'Reading uploaded file')
        return response.content

    @staticmethod
    def validate_tex_path(tex_path):
        """Accept exact, project-relative POSIX paths to standalone TeX documents."""
        path = PurePosixPath(tex_path)
        if not tex_path or path.is_absolute() or '..' in path.parts or '\\' in tex_path or path.suffix.lower() != '.tex':
            raise ValueError("The TeX path must be a project-relative .tex path without '..'.")
        return path.as_posix()

    @staticmethod
    def document_paths(project_infos):
        """Map full TeX paths to document IDs, including nested folders."""
        return {path: entry['id'] for path, entry in PLMlatexClient.project_entries(project_infos).items()
                if entry['kind'] == 'doc' and path.lower().endswith('.tex')}

    @staticmethod
    def _check_response(response, operation):
        # Do not include response bodies, cookies, or signed URLs in errors.
        if 300 <= response.status_code < 400 or response.status_code in (401, 403):
            raise AuthenticationError(operation + ": authentication or permission failure; renew the login session.")
        if not response.ok:
            raise CompilationError("{}: HTTP {}.".format(operation, response.status_code))

    def refresh_csrf(self, project_id, timeout=30):
        """Validate the saved session and obtain a current CSRF token."""
        response = reqs.get(
            self._base_url + '/project/' + project_id,
            cookies=self._cookie, timeout=timeout, allow_redirects=False,
        )
        self._check_response(response, 'Opening project')
        token = BeautifulSoup(response.content, 'html.parser').find('meta', {'name': 'ol-csrfToken'})
        if token is None or not token.get('content'):
            raise AuthenticationError("No CSRF token on the project page; renew the login session.")
        self._csrf = token['content']

    def compile_pdf(self, project_id, tex_path, project_infos=None, timeout=180, with_dependencies=False):
        """Compile an explicit TeX path and return its PDF and optional recorder artifacts.

        Root selection applies only to this compile request; no settings are saved.
        """
        tex_path = self.validate_tex_path(tex_path)
        if project_infos is None:
            project_infos = self.get_project_infos(project_id)
        documents = self.document_paths(project_infos)
        if tex_path not in documents:
            raise CompilationError("TeX document not found in project: " + tex_path)
        headers = {"X-Csrf-Token": self._csrf}
        body = {
            "check": "silent",
            "draft": False,
            "incrementalCompilesEnabled": False,
            # Older Overleaf versions use the ID; newer versions need the path.
            "rootDoc_id": documents[tex_path],
            "rootResourcePath": tex_path,
            "stopOnFirstError": True
        }

        for attempt in range(3):
            r = reqs.post(
                self._base_url + '/project/' + project_id + '/compile?enable_pdf_caching=true',
                cookies=self._cookie, headers=headers, json=body,
                timeout=timeout, allow_redirects=False,
            )
            self._check_response(r, 'Compiling project')
            try:
                compile_result = r.json()
            except ValueError as error:
                raise CompilationError("Compilation returned invalid JSON; check the login session.") from error
            if not isinstance(compile_result, dict):
                raise CompilationError("Compilation returned an unexpected response.")
            status = compile_result.get('status')
            if status in ('too-recently-compiled', 'autocompile-backoff', 'compile-in-progress') and attempt < 2:
                time.sleep(2 ** (attempt + 1))
                continue
            if status != 'success':
                raise CompilationError("Compilation did not succeed (status: {!r}); no PDF downloaded.".format(status))
            break

        outputs = compile_result.get('outputFiles', [])
        if not isinstance(outputs, list):
            raise CompilationError("Compilation returned an invalid output list.")
        pdf_files = [v for v in outputs if isinstance(v, dict) and v.get('type') == 'pdf']
        # Overleaf builds with -jobname=output. EPS conversion may create other
        # PDFs in the same response; these are graphics, not the main document.
        main_pdfs = [output for output in pdf_files if output.get('path') == 'output.pdf']
        if len(main_pdfs) == 1:
            pdf_file = main_pdfs[0]
        elif not main_pdfs and len(pdf_files) == 1:
            # Retain compatibility with servers returning one differently named PDF.
            pdf_file = pdf_files[0]
        else:
            raise CompilationError("Could not identify one main PDF among {} PDF outputs.".format(len(pdf_files)))
        if not isinstance(pdf_file.get('url'), str) or not isinstance(pdf_file.get('path'), str):
            raise CompilationError("The compilation response is missing the PDF URL or path.")
        pdf_content = self.download_output(pdf_file['url'], timeout)
        if not pdf_content.startswith(b'%PDF-'):
            raise CompilationError("The downloaded response is not a PDF.")
        artifacts = {}
        if with_dependencies:
            for output in outputs:
                if isinstance(output, dict) and output.get('type') in ('fls', 'fdb_latexmk'):
                    artifacts[output['path']] = self.download_output(output['url'], timeout)
        return {'path': pdf_file['path'], 'content': pdf_content, 'artifacts': artifacts}

    def download_output(self, url, timeout=180):
        pdf_url = urljoin(self._base_url + '/', url)
        origin, target = urlsplit(self._base_url), urlsplit(pdf_url)
        if (origin.scheme, origin.netloc) != (target.scheme, target.netloc):
            raise CompilationError("The PDF URL points to a different server; refusing to send login cookies.")
        download_req = reqs.get(pdf_url, cookies=self._cookie, timeout=timeout, allow_redirects=False)
        self._check_response(download_req, 'Downloading PDF')
        return download_req.content
