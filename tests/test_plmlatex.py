"""Offline regression tests; these do not establish live PLMlatex compatibility."""

import contextlib
import copy
import html
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from plm_moodle_sync.plmlatex.fetch import compile_documents
from plm_moodle_sync.cli import main
from plm_moodle_sync.plmlatex.client import PLMlatexClient
from plm_moodle_sync.plmlatex.errors import CompilationError


PROJECT_ID = '0123456789abcdef01234567'
TREE = {
    'rootDoc_id': 'notes-id',
    'rootFolder': [{
        'docs': [{'name': 'notes.tex', '_id': 'notes-id'}],
        'folders': [{
            'name': 'td',
            'docs': [{'name': 'main.tex', '_id': 'td-id'}],
            'folders': [{
                'name': 'solutions',
                'docs': [{'name': 'main.tex', '_id': 'solutions-id'}],
                'folders': [],
            }],
        }],
    }],
}


def response(data=None, content=None, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = content if content is not None else json.dumps(data).encode()
    return result


def compiled(build='build-1'):
    return response({
        'status': 'success',
        'outputFiles': [{'type': 'pdf', 'path': 'output.pdf', 'url': '/build/' + build + '/output.pdf'}],
    })


class CompilationTests(unittest.TestCase):
    def setUp(self):
        self.client = PLMlatexClient({'sharelatex.sid': 'test-cookie'})

    def test_nested_paths_distinguish_documents_with_same_basename(self):
        self.assertEqual(self.client.document_paths(TREE), {
            'notes.tex': 'notes-id', 'td/main.tex': 'td-id',
            'td/solutions/main.tex': 'solutions-id',
        })

    def test_invalid_or_missing_path_never_starts_compilation(self):
        with patch('plm_moodle_sync.plmlatex.client.reqs.post') as post:
            for path in ('../notes.tex', '/notes.tex', 'td/../../notes.tex', 'notes.pdf', 'td\\main.tex', ''):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    self.client.compile_pdf(PROJECT_ID, path, TREE)
            with self.assertRaises(CompilationError):
                self.client.compile_pdf(PROJECT_ID, 'TD/main.tex', TREE)
            post.assert_not_called()

    def test_each_document_gets_its_own_build_without_mutating_metadata(self):
        tree = copy.deepcopy(TREE)
        with patch('plm_moodle_sync.plmlatex.client.reqs.post', side_effect=[compiled('one'), compiled('two')]) as post, \
             patch('plm_moodle_sync.plmlatex.client.reqs.get', side_effect=[response(content=b'%PDF-1.7 TD'), response(content=b'%PDF-1.7 solutions')]) as get:
            first = self.client.compile_pdf(PROJECT_ID, 'td/main.tex', tree)
            second = self.client.compile_pdf(PROJECT_ID, 'td/solutions/main.tex', tree)
        self.assertNotEqual(first['content'], second['content'])
        self.assertEqual(first['path'], second['path'])  # Both remote artifacts are output.pdf.
        payloads = [call.kwargs['json'] for call in post.call_args_list]
        self.assertEqual([p['rootDoc_id'] for p in payloads], ['td-id', 'solutions-id'])
        self.assertEqual([p['rootResourcePath'] for p in payloads], ['td/main.tex', 'td/solutions/main.tex'])
        self.assertTrue(all(not p['incrementalCompilesEnabled'] for p in payloads))
        self.assertTrue(all('/compile?' in call.args[0] for call in post.call_args_list))
        self.assertIn('/build/one/', get.call_args_list[0].args[0])
        self.assertIn('/build/two/', get.call_args_list[1].args[0])
        self.assertEqual(tree, TREE)

    def test_failed_compile_never_downloads_an_old_pdf(self):
        failed = {'status': 'error', 'outputFiles': [{'type': 'pdf', 'path': 'output.pdf', 'url': '/old.pdf'}]}
        with patch('plm_moodle_sync.plmlatex.client.reqs.post', return_value=response(failed)), \
             patch('plm_moodle_sync.plmlatex.client.reqs.get') as get, self.assertRaises(CompilationError):
            self.client.compile_pdf(PROJECT_ID, 'notes.tex', TREE)
        get.assert_not_called()

    def test_transient_compile_status_has_bounded_retries(self):
        busy = response({'status': 'too-recently-compiled'})
        with patch('plm_moodle_sync.plmlatex.client.reqs.post', side_effect=[busy, compiled()]) as post, \
             patch('plm_moodle_sync.plmlatex.client.reqs.get', return_value=response(content=b'%PDF-1.7 ok')), \
             patch('plm_moodle_sync.plmlatex.client.time.sleep') as sleep:
            self.client.compile_pdf(PROJECT_ID, 'notes.tex', TREE)
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(2)
        with patch('plm_moodle_sync.plmlatex.client.reqs.post', return_value=busy) as post, \
             patch('plm_moodle_sync.plmlatex.client.time.sleep'), self.assertRaises(CompilationError):
            self.client.compile_pdf(PROJECT_ID, 'notes.tex', TREE)
        self.assertEqual(post.call_count, 3)

    def test_timeout_does_not_blindly_resubmit_a_compile(self):
        with patch('plm_moodle_sync.plmlatex.client.reqs.post', side_effect=requests.Timeout) as post, self.assertRaises(requests.Timeout):
            self.client.compile_pdf(PROJECT_ID, 'notes.tex', TREE)
        post.assert_called_once()

    def test_rejects_ambiguous_missing_or_external_pdf(self):
        pdf = {'type': 'pdf', 'path': 'output.pdf', 'url': '/out.pdf'}
        for outputs in ([], [pdf, pdf], [{**pdf, 'url': 'https://other.example/out.pdf'}]):
            with self.subTest(outputs=outputs), \
                 patch('plm_moodle_sync.plmlatex.client.reqs.post', return_value=response({'status': 'success', 'outputFiles': outputs})), \
                 patch('plm_moodle_sync.plmlatex.client.reqs.get') as get, self.assertRaises(CompilationError):
                self.client.compile_pdf(PROJECT_ID, 'notes.tex', TREE)
            get.assert_not_called()

    def test_rejects_login_page_as_pdf(self):
        with patch('plm_moodle_sync.plmlatex.client.reqs.post', return_value=compiled()), \
             patch('plm_moodle_sync.plmlatex.client.reqs.get', return_value=response(content=b'<html>Login</html>')), self.assertRaises(CompilationError):
            self.client.compile_pdf(PROJECT_ID, 'notes.tex', TREE)

    def test_selects_main_pdf_among_converted_eps_graphics(self):
        outputs = [
            {'type': 'pdf', 'path': 'TD/figure-eps-converted-to.pdf', 'url': '/build/figure.pdf'},
            {'type': 'pdf', 'path': 'output.pdf', 'url': '/build/output.pdf'},
            {'type': 'pdf', 'path': 'TD/second-eps-converted-to.pdf', 'url': '/build/second.pdf'},
        ]
        with patch('plm_moodle_sync.plmlatex.client.reqs.post', return_value=response({'status': 'success', 'outputFiles': outputs})), \
             patch('plm_moodle_sync.plmlatex.client.reqs.get', return_value=response(content=b'%PDF-main-document')) as get:
            result = self.client.compile_pdf(PROJECT_ID, 'notes.tex', TREE)
        self.assertEqual(result['path'], 'output.pdf')
        self.assertEqual(result['content'], b'%PDF-main-document')
        get.assert_called_once()
        self.assertTrue(get.call_args.args[0].endswith('/build/output.pdf'))

    def test_multiple_graphics_without_a_main_pdf_are_ambiguous(self):
        outputs = [
            {'type': 'pdf', 'path': 'figure.pdf', 'url': '/figure.pdf'},
            {'type': 'pdf', 'path': 'another.pdf', 'url': '/another.pdf'},
        ]
        with patch('plm_moodle_sync.plmlatex.client.reqs.post', return_value=response({'status': 'success', 'outputFiles': outputs})), \
             patch('plm_moodle_sync.plmlatex.client.reqs.get') as get, self.assertRaises(CompilationError):
            self.client.compile_pdf(PROJECT_ID, 'notes.tex', TREE)
        get.assert_not_called()

    def test_authentication_redirects_are_not_followed(self):
        with patch('plm_moodle_sync.plmlatex.client.reqs.post', return_value=response(status=302)) as post, self.assertRaises(CompilationError):
            self.client.compile_pdf(PROJECT_ID, 'notes.tex', TREE)
        self.assertFalse(post.call_args.kwargs['allow_redirects'])

    def test_refreshes_csrf_and_honors_configured_server(self):
        client = PLMlatexClient({'sharelatex.sid': 'cookie'}, base_url='https://latex.example/')
        page = response(content=b'<meta name="ol-csrfToken" content="fresh-token">')
        with patch('plm_moodle_sync.plmlatex.client.reqs.get', side_effect=[page, response(content=b'%PDF-1.7 ok')]) as get, \
             patch('plm_moodle_sync.plmlatex.client.reqs.post', return_value=compiled()) as post:
            client.refresh_csrf(PROJECT_ID)
            client.compile_pdf(PROJECT_ID, 'notes.tex', TREE)
        self.assertTrue(all(call.args[0].startswith('https://latex.example/') for call in get.call_args_list))
        self.assertTrue(post.call_args.args[0].startswith('https://latex.example/'))
        self.assertEqual(post.call_args.kwargs['headers']['X-Csrf-Token'], 'fresh-token')

    def test_metadata_wait_is_bounded_and_socket_is_closed(self):
        socket = Mock(connected=True)
        def receive_project(event, args, callback):
            # The real legacy client cannot dispatch packets without a namespace.
            socket.on.assert_called_once()
            self.assertEqual(socket.on.call_args.args[0], 'connect')
            callback(None, TREE)
        socket.emit.side_effect = receive_project
        with patch('plm_moodle_sync.plmlatex.client.ProjectSocket', return_value=socket):
            self.assertEqual(self.client.get_project_infos(PROJECT_ID, timeout=7), TREE)
        socket.wait_for_callbacks.assert_called_once_with(seconds=7)
        socket.disconnect.assert_called_once()

    def test_project_name_lookup_ignores_archived_and_trashed_projects(self):
        projects = [
            {'name': 'test', 'id': PROJECT_ID},
            {'name': 'test', 'id': 'old', 'archived': True},
            {'name': 'test', 'id': 'deleted', 'trashed': True},
        ]
        page = '<meta name="ol-projects" content="' + html.escape(json.dumps(projects), quote=True) + '">'
        client = PLMlatexClient({'sid': 'test'}, base_url='https://latex.example')
        with patch('plm_moodle_sync.plmlatex.client.reqs.get', return_value=response(content=page.encode())) as get:
            self.assertEqual(client.get_project('test')['id'], PROJECT_ID)
            self.assertIsNone(client.get_project('TEST'))
        self.assertEqual(get.call_args.args[0], 'https://latex.example/project')
        self.assertFalse(get.call_args.kwargs['allow_redirects'])

    def test_project_name_lookup_rejects_duplicate_names(self):
        with patch.object(self.client, 'all_projects', return_value=[
            {'name': 'test', 'id': PROJECT_ID}, {'name': 'test', 'id': 'another-id'},
        ]), self.assertRaisesRegex(CompilationError, 'Several projects'):
            self.client.get_project('test')

    def test_project_name_lookup_reports_expired_session(self):
        with patch('plm_moodle_sync.plmlatex.client.reqs.get', return_value=response(content=b'<html>Login</html>')), \
             self.assertRaisesRegex(CompilationError, 'renew the login session'):
            self.client.get_project('test')


class FetchTests(unittest.TestCase):
    def fake_client(self):
        client = Mock(spec=PLMlatexClient)
        client.base_url = 'https://latex.example'
        client.validate_tex_path.side_effect = PLMlatexClient.validate_tex_path
        client.document_paths.side_effect = PLMlatexClient.document_paths
        client.project_entries.side_effect = PLMlatexClient.project_entries
        client.get_project_infos.return_value = copy.deepcopy(TREE)
        @contextlib.contextmanager
        def connection(project_id):
            yield Mock(), copy.deepcopy(TREE)
        client.project_connection.side_effect = connection
        client.read_document.return_value = {'version': 0, 'text': '\\documentclass{article}'}
        client.compile_pdf.side_effect = [
            {'path': 'output.pdf', 'content': b'%PDF-1.7 TD', 'artifacts': {}},
            {'path': 'output.pdf', 'content': b'%PDF-1.7 solutions', 'artifacts': {}},
        ]
        return client

    def test_two_same_basename_documents_are_saved_separately(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            destination = Path(directory)
            report = compile_documents(self.fake_client(), PROJECT_ID, ['td/main.tex', 'td/solutions/main.tex'], destination, 90, state_file=destination / 'state.json')
            self.assertEqual((destination / PROJECT_ID / 'td/main.pdf').read_bytes(), b'%PDF-1.7 TD')
            self.assertEqual((destination / PROJECT_ID / 'td/solutions/main.pdf').read_bytes(), b'%PDF-1.7 solutions')
            self.assertEqual(report['main_document_before'], report['main_document_after'])
            state = json.loads((destination / 'state.json').read_text())
            self.assertEqual(list(state['projects'].values())[0]['builds']['td/main.tex'], report['files'][0])
            self.assertFalse((destination / PROJECT_ID / 'manifest.json').exists())

    def test_failure_preserves_previously_saved_pdf(self):
        client = self.fake_client()
        client.compile_pdf.side_effect = [{'path': 'output.pdf', 'content': b'%PDF-new', 'artifacts': {}}, CompilationError('Failed')]
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            destination = Path(directory)
            previous = destination / PROJECT_ID / 'td/main.pdf'
            previous.parent.mkdir(parents=True)
            previous.write_bytes(b'%PDF-old')
            with self.assertRaises(CompilationError):
                compile_documents(client, PROJECT_ID, ['td/main.tex', 'td/solutions/main.tex'], destination, 90, state_file=destination / 'state.json')
            self.assertEqual(previous.read_bytes(), b'%PDF-old')
            self.assertFalse((destination / PROJECT_ID / 'manifest.json').exists())

    def test_main_document_change_is_reported_before_saving(self):
        client = self.fake_client()
        client.get_project_infos.return_value = {**TREE, 'rootDoc_id': 'someone-changed-it'}
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            destination = Path(directory)
            with self.assertRaises(CompilationError):
                compile_documents(client, PROJECT_ID, ['notes.tex'], destination, 90, state_file=destination / 'state.json')
            self.assertEqual(list(destination.rglob('*.pdf')), [])
            self.assertFalse((destination / 'state.json').exists())

    def test_network_error_does_not_print_signed_url(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch('plm_moodle_sync.sync.load_session', return_value={'sid': 'test'}), \
             patch('plm_moodle_sync.sync.PLMlatexClient.refresh_csrf', side_effect=requests.Timeout('https://example/?secret=value')), \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = main(['fetch', '--project-id', PROJECT_ID, '--list-documents', '--log-file', str(Path(directory) / 'test.log'),
                           '--state-file', str(Path(directory) / 'state.json')])
        self.assertEqual(result, 1)
        self.assertIn('network request failed', stderr.getvalue())
        self.assertNotIn('secret', stderr.getvalue())

    def test_cli_resolves_project_name_before_compiling(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch('plm_moodle_sync.sync.load_session', return_value={'sid': 'test'}), \
             patch('plm_moodle_sync.sync.PLMlatexClient') as client_class, \
             patch('plm_moodle_sync.sync.compile_documents') as compile_call, \
             contextlib.redirect_stdout(io.StringIO()):
            client_class.return_value.get_project.return_value = {'id': PROJECT_ID, 'name': 'test'}
            result = main(['fetch', '--project-name', 'test', '--cookie-file', str(Path(directory) / 'plmlatex.json'), '--tex', 'TD/TD1.tex', '--tex', 'TD/TD2.tex',
                           '--log-file', str(Path(directory) / 'test.log'),
                           '--state-file', str(Path(directory) / 'state.json'),
                           '--output-dir', str(Path(directory) / 'cache')])
        self.assertEqual(result, 0)
        self.assertEqual(compile_call.call_args.args[1:3], (PROJECT_ID, ['TD/TD1.tex', 'TD/TD2.tex']))


if __name__ == '__main__':
    unittest.main()
