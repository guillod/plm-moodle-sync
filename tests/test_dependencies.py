"""TeX dependency discovery and incremental compilation with synthetic projects."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from plm_moodle_sync.plmlatex.fetch import compile_documents, source_locations
from plm_moodle_sync.plmlatex.dependencies import discover, recorder_inputs
from plm_moodle_sync.plmlatex.client import PLMlatexClient
from plm_moodle_sync.plmlatex.errors import CompilationError


class DependencyParsingTests(unittest.TestCase):
    def test_follows_literal_inputs_packages_bibliography_and_graphics(self):
        source = r'''
        % \input{ignore}
        \input{preamble}
        \include{parts/problem}
        \usepackage{custom,amsmath}
        \graphicspath{{figures/}}
        \includegraphics[width=4cm]{plot}
        \bibliography{refs}
        '''
        available = {'TD/preamble.tex', 'TD/parts/problem.tex', 'custom.sty', 'TD/figures/plot.png', 'refs.bib', 'ignore.tex'}
        dependencies, uncertain = discover(source, 'TD/td1.tex', 'TD/td1.tex', available)
        self.assertEqual(dependencies, available - {'ignore.tex'})
        self.assertEqual(uncertain, [])

    def test_subfiles_and_import_paths(self):
        dependencies, _ = discover(r'\documentclass[../main.tex]{subfiles}\subimport{../shared/}{macros}', 'TD/td.tex', 'TD/td.tex', {'main.tex', 'shared/macros.tex'})
        self.assertEqual(dependencies, {'main.tex', 'shared/macros.tex'})

    def test_extensionless_graphics_resolve_to_eps_and_png(self):
        source = r'''
        \input{preamble}
        \includegraphics[clip=true, trim=3cm 0cm 3cm 0cm, width=\linewidth]{plot1}
        \noindent\includegraphics[width=\linewidth]{plot2}
        \includegraphics[width=0.7\linewidth]{diagram}
        '''
        available = {'TD/preamble.tex', 'TD/plot1.eps', 'TD/plot2.eps', 'TD/diagram.png'}
        dependencies, uncertain = discover(source, 'TD/graphics.tex', 'TD/graphics.tex', available)
        self.assertEqual(dependencies, available)
        self.assertEqual(uncertain, [])

    def test_dynamic_input_is_reported(self):
        dependencies, uncertain = discover(r'\input{\selectedfile}', 'main.tex', 'main.tex', {})
        self.assertEqual(dependencies, set())
        self.assertTrue(uncertain)

    def test_literal_file_wrapper_tracks_both_conditional_definitions(self):
        source = r'''
        \iftikz
          \newcommand{\includefigure}[1]{\par\begingroup\centering
            \tikzsetnextfilename{#1}\input{figures/#1.tex}\endgroup}
        \else
          \newcommand{\includefigure}[1]{\includegraphics[width=\linewidth]{figures/#1.pdf}}
        \fi
        \includefigure{pendule}
        % \includefigure{unused}
        '''
        available = {'Poly/figures/pendule.tex', 'Poly/figures/pendule.pdf',
                     'Poly/figures/unused.tex', 'TD/td1.tex'}
        dependencies, uncertain = discover(source, 'Poly/main.tex', 'Poly/main.tex', available)
        self.assertEqual(dependencies, {'Poly/figures/pendule.tex', 'Poly/figures/pendule.pdf'})
        self.assertEqual(uncertain, [])

    def test_literal_wrapper_arguments_can_resolve_relative_parent_paths(self):
        source = r'\newcommand\figurefile[1]{\includegraphics{figures/#1}}\figurefile{../../shared/logo.pdf}'
        dependencies, uncertain = discover(source, 'Poly/main.tex', 'Poly/main.tex', {'shared/logo.pdf'})
        self.assertEqual(dependencies, {'shared/logo.pdf'})
        self.assertEqual(uncertain, [])

    def test_unsupported_file_wrappers_keep_conservative_tracking(self):
        for source in (
            r'\newcommand{\figurefile}[1]{\includegraphics{#1}}\figurefile{\chosen}',
            r'\newcommand{\figurefile}[1]{\includegraphics{#1}}\let\alias\figurefile\alias{logo.pdf}',
            r'\newcommand{\figurefile}[1]{\includegraphics{#1}}',
            r'\newcommand{\figurefile}[1][default]{\includegraphics{#1}}\figurefile{logo.pdf}',
            r'\newcommand{\figurefile}[2]{\includegraphics{#1/#2}}\figurefile{figures}{logo.pdf}',
            r'\newcommand{\figurefile}[1]{\includegraphics{#1}\figurefile{other}}\figurefile{logo.pdf}',
            r'\newcommand{\figurefile}[1]{\includegraphics{#1}}\def\figurefile#1{\includegraphics{#1}}\figurefile{logo.pdf}',
            r'\newcommand{\figurefile}[1]{\unknownhelper{#1}\includegraphics{#1}}\figurefile{logo.pdf}',
        ):
            with self.subTest(source=source):
                _, uncertain = discover(source, 'main.tex', 'main.tex', {'logo.pdf'})
                self.assertTrue(uncertain)

    def test_recorder_resolves_relative_and_absolute_paths_without_system_files(self):
        artifacts = {'output.fls': b'PWD /compile/TD\nINPUT td1.tex\nINPUT /compile/TD/preamble.tex\nINPUT /usr/share/texmf/article.cls\nINPUT ../shared/logo.png\n'}
        available = {'TD/td1.tex', 'TD/preamble.tex', 'shared/logo.png'}
        self.assertEqual(recorder_inputs(artifacts, 'TD/td1.tex', available), available)


class FakeProject(PLMlatexClient):
    def __init__(self):
        super().__init__({}, base_url='https://latex.example')
        self.texts = {
            'TD/td1.tex': r'\input{preamble} First',
            'TD/td2.tex': r'\input{preamble} Second',
            'TD/preamble.tex': r'\usepackage{amsmath}',
            'TD/unused.tex': 'Unused',
        }
        self.versions = {path: 0 for path in self.texts}
        self.compiled = []
        self.reads = []
        self.artifacts = {}
        self.fail = False
        self.change_during_compile = False
        self.uploads = {}

    def tree(self):
        return {'rootDoc_id': 'saved-main', 'compiler': 'pdflatex', 'rootFolder': [{
            'folders': [{'name': 'TD', 'docs': [{'name': path[3:], '_id': path} for path in self.texts],
                         'fileRefs': [{'name': path[3:], '_id': path} for path in self.uploads]}],
        }]}

    def refresh_csrf(self, project_id):
        pass

    @contextlib.contextmanager
    def project_connection(self, project_id):
        yield object(), self.tree()

    def get_project_infos(self, project_id):
        return self.tree()

    def read_document(self, socket, doc_id, previous_version=-1):
        self.reads.append(doc_id)
        version = self.versions[doc_id]
        return {'version': version, 'text': self.texts[doc_id] if version != previous_version else None}

    def compile_pdf(self, project_id, root, tree, timeout, with_dependencies=False):
        if self.fail:
            raise CompilationError('Compilation failed')
        self.compiled.append(root)
        if self.change_during_compile:
            self.versions[root] += 1
        return {'path': 'output.pdf', 'content': ('%PDF-' + root + str(self.versions)).encode(), 'artifacts': self.artifacts}

    def download_file(self, project_id, file_id):
        return self.uploads[file_id]


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.client = FakeProject()
        self.roots = ['TD/td1.tex', 'TD/td2.tex']

    def run_sync(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return compile_documents(self.client, 'project-id', self.roots, self.directory / 'cache', 30,
                                     state_file=self.directory / 'state.json', **kwargs)

    def edit(self, path):
        self.client.texts[path] += '\n% Edited content'
        self.client.versions[path] += 1

    def test_first_run_builds_then_unchanged_run_skips_everything(self):
        first = self.run_sync()
        self.assertEqual(first['compiled'], self.roots)
        self.client.compiled.clear()
        self.client.reads.clear()
        second = self.run_sync()
        self.assertEqual(second['skipped'], self.roots)
        self.assertEqual(self.client.compiled, [])
        self.assertEqual(self.client.reads.count('TD/preamble.tex'), 1)
        self.assertNotIn('TD/unused.tex', self.client.reads)
        inputs = first['files'][0]['inputs']
        self.assertEqual(set(inputs), {'TD/td1.tex', 'TD/preamble.tex'})

    def test_root_change_rebuilds_one_and_shared_change_rebuilds_both(self):
        self.run_sync()
        self.edit('TD/td1.tex')
        self.assertEqual(self.run_sync()['compiled'], ['TD/td1.tex'])
        self.edit('TD/preamble.tex')
        self.assertEqual(self.run_sync()['compiled'], self.roots)

    def test_revision_only_change_updates_records_without_rebuilding(self):
        self.run_sync()
        self.client.versions['TD/td1.tex'] += 1
        result = self.run_sync()
        self.assertEqual(result['compiled'], [])
        self.assertEqual(result['files'][0]['inputs']['TD/td1.tex']['version'], 1)
        self.assertEqual(self.run_sync()['compiled'], [])

    def test_missing_operation_history_preserves_content_based_rebuilds(self):
        self.run_sync()
        wire = Mock()
        requests = []

        def emit(event, *args):
            if event != 'joinDoc':
                return
            path, requested, _, callback = args
            version = self.client.versions[path]
            requests.append((path, requested))
            if requested != -1 and requested != version:
                callback({'message': 'doc updater could not load requested ops'})
            else:
                lines = self.client.texts[path].split('\n') if requested == -1 else None
                callback(None, lines, version)

        wire.emit.side_effect = emit
        def read_document(socket, doc_id, previous_version=-1):
            return PLMlatexClient.read_document(wire, doc_id, previous_version)

        with patch.object(self.client, 'read_document', side_effect=read_document):
            for changed in (False, True):
                with self.subTest(content_changed=changed):
                    self.client.versions['TD/td1.tex'] += 1
                    if changed:
                        self.client.texts['TD/td1.tex'] += '\n% Changed content'
                    requests.clear()
                    result = self.run_sync()
                    self.assertEqual(result['compiled'], ['TD/td1.tex'] if changed else [])
                    self.assertIn(('TD/td1.tex', -1), requests)
                    self.assertNotIn(('TD/td2.tex', -1), requests)
                    self.assertEqual(result['files'][0]['inputs']['TD/td1.tex']['version'],
                                     self.client.versions['TD/td1.tex'])
                    requests.clear()
                    self.assertEqual(self.run_sync()['compiled'], [])
                    self.assertTrue(all(version != -1 for _, version in requests))

    def test_revision_errors_name_the_source_before_compilation(self):
        with patch.object(self.client, 'read_document', side_effect=CompilationError('Revision read failed')):
            with self.assertRaisesRegex(CompilationError, 'PLMlatex source TD/td1.tex: Revision read failed'):
                self.run_sync()
        self.assertEqual(self.client.compiled, [])
        self.assertFalse((self.directory / 'state.json').exists())

    def test_revision_errors_name_the_source_during_build_verification(self):
        compile_pdf = self.client.compile_pdf
        def compile_then_fail(*args, **kwargs):
            result = compile_pdf(*args, **kwargs)
            self.client.read_document = Mock(side_effect=CompilationError('Revision read failed'))
            return result
        with patch.object(self.client, 'compile_pdf', side_effect=compile_then_fail):
            with self.assertRaisesRegex(CompilationError, 'PLMlatex source TD/preamble.tex: Revision read failed'):
                self.run_sync()
        self.assertFalse((self.directory / 'state.json').exists())
        self.assertEqual(list(self.directory.rglob('*.pdf')), [])

    def test_unrelated_addition_rename_and_removal_do_not_rebuild(self):
        self.run_sync()
        self.client.uploads['TD/new-image.png'] = b'unrelated'
        first = self.run_sync()
        self.assertEqual(first['compiled'], [])
        self.client.texts['TD/renamed.tex'] = self.client.texts.pop('TD/unused.tex')
        self.client.versions['TD/renamed.tex'] = self.client.versions.pop('TD/unused.tex')
        second = self.run_sync()
        self.assertEqual(second['compiled'], [])
        self.assertNotEqual(first['files'][0]['structure'], second['files'][0]['structure'])
        del self.client.texts['TD/renamed.tex']
        self.assertEqual(self.run_sync()['compiled'], [])
        self.assertEqual(self.run_sync()['compiled'], [])

    def test_new_local_package_shadowing_system_package_rebuilds_dependents(self):
        self.run_sync()
        self.client.texts['TD/amsmath.sty'] = '% Local override'
        self.client.versions['TD/amsmath.sty'] = 0
        self.assertEqual(self.run_sync()['compiled'], self.roots)
        self.assertEqual(self.run_sync()['compiled'], [])

    def test_optional_file_appearing_rebuilds_only_its_user(self):
        self.client.texts['TD/td1.tex'] += r'\IfFileExists{optional.tex}{yes}{no}'
        self.run_sync()
        self.client.texts['TD/optional.tex'] = 'New file'
        self.client.versions['TD/optional.tex'] = 0
        self.assertEqual(self.run_sync()['compiled'], ['TD/td1.tex'])
        self.assertEqual(self.run_sync()['compiled'], [])

    def test_removing_a_watched_input_cannot_reuse_its_pdf(self):
        self.run_sync()
        del self.client.texts['TD/preamble.tex']
        self.assertEqual(self.run_sync()['compiled'], self.roots)

    def test_metadata_only_uploaded_file_replacement_does_not_rebuild(self):
        self.client.uploads['TD/logo.png'] = b'unchanged image'
        self.client.texts['TD/td1.tex'] += r'\includegraphics{logo}'
        self.run_sync()
        original_tree = self.client.tree

        def with_revision():
            tree = original_tree()
            tree['rootFolder'][0]['folders'][0]['fileRefs'][0]['rev'] = 'next-revision'
            return tree

        with patch.object(self.client, 'tree', side_effect=with_revision):
            self.assertEqual(self.run_sync()['compiled'], [])

    def test_refined_macro_discovery_rebuilds_once_then_ignores_unrelated_edits(self):
        self.roots = ['TD/td1.tex']
        self.client.texts['TD/td1.tex'] += r'\newcommand{\figurefile}[1]{\includegraphics{#1}}\figurefile{logo}'
        self.client.uploads['TD/logo.png'] = b'figure'
        with patch('plm_moodle_sync.plmlatex.dependencies.expand_file_wrappers', side_effect=lambda text: text):
            old = self.run_sync()['files'][0]
        self.assertTrue(old['conservative_project_scan'])
        updated = self.run_sync()
        self.assertEqual(updated['compiled'], self.roots)
        self.assertFalse(updated['files'][0]['conservative_project_scan'])
        self.assertEqual(self.run_sync()['compiled'], [])
        self.edit('TD/unused.tex')
        self.assertEqual(self.run_sync()['compiled'], [])
        self.client.uploads['TD/logo.png'] = b'changed figure'
        self.assertEqual(self.run_sync()['compiled'], self.roots)

    def test_uncertain_runtime_dependency_does_not_cause_endless_refinement(self):
        self.client.texts['TD/unused.tex'] = r'\input{\dynamic}'
        self.client.artifacts = {'output.fls': b'PWD /compile/TD\nINPUT unused.tex\n'}
        result = self.run_sync()
        self.assertTrue(all(file['conservative_project_scan'] for file in result['files']))
        self.assertEqual(self.run_sync()['compiled'], [])
        self.assertEqual(self.run_sync()['compiled'], [])

    def test_rebuild_log_identifies_changed_inputs_and_missing_outputs(self):
        self.run_sync()
        self.edit('TD/preamble.tex')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            compile_documents(self.client, 'project-id', self.roots, self.directory / 'cache', 30,
                              state_file=self.directory / 'state.json')
        self.assertIn('input content changed: TD/preamble.tex', output.getvalue())
        (self.directory / 'cache/project-id/TD/td1.pdf').unlink()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            compile_documents(self.client, 'project-id', self.roots, self.directory / 'cache', 30,
                              state_file=self.directory / 'state.json')
        self.assertIn('cached PDF missing', output.getvalue())

    def test_dependencies_are_rediscovered_after_source_change(self):
        self.run_sync()
        self.client.texts['TD/td1.tex'] = r'\input{unused} First'
        self.edit('TD/td1.tex')
        result = self.run_sync()
        self.assertEqual(set(result['files'][0]['inputs']), {'TD/td1.tex', 'TD/unused.tex'})
        self.edit('TD/preamble.tex')
        self.assertEqual(self.run_sync()['compiled'], ['TD/td2.tex'])

    def test_cyclic_includes_terminate(self):
        self.client.texts['TD/preamble.tex'] = r'\input{td1}'
        self.assertEqual(self.run_sync()['compiled'], self.roots)

    def test_new_runtime_dependency_is_watched_on_future_runs(self):
        self.client.artifacts = {'output.fls': b'PWD /compile/TD\nINPUT unused.tex\n'}
        result = self.run_sync()
        self.assertIn('TD/unused.tex', result['files'][0]['inputs'])
        self.assertEqual(self.run_sync()['compiled'], [])
        self.edit('TD/unused.tex')
        self.assertEqual(self.run_sync()['compiled'], self.roots)

    def test_uploaded_dependency_replacement_invalidates_pdf(self):
        self.client.uploads['TD/logo.png'] = b'first-image'
        self.client.texts['TD/preamble.tex'] += r'\includegraphics{logo}'
        self.run_sync()
        self.assertEqual(self.run_sync()['compiled'], [])
        self.client.uploads['TD/logo.png'] = b'second-image'
        self.assertEqual(self.run_sync()['compiled'], self.roots)

    def test_failed_build_does_not_advance_state_or_replace_pdf(self):
        self.run_sync()
        state_path = self.directory / 'state.json'
        old_state = state_path.read_bytes()
        pdf = self.directory / 'cache/project-id/TD/td1.pdf'
        old_pdf = pdf.read_bytes()
        self.edit('TD/td1.tex')
        self.client.fail = True
        with self.assertRaises(CompilationError):
            self.run_sync()
        self.assertEqual(state_path.read_bytes(), old_state)
        self.assertEqual(pdf.read_bytes(), old_pdf)

    def test_concurrent_edit_rejects_build(self):
        self.client.change_during_compile = True
        with self.assertRaisesRegex(CompilationError, 'changed during compilation'):
            self.run_sync()
        self.assertFalse((self.directory / 'state.json').exists())

    def test_dynamic_inputs_use_conservative_project_tracking(self):
        self.client.texts['TD/td1.tex'] += r'\input{\dynamic}'
        result = self.run_sync()
        self.assertTrue(result['files'][0]['conservative_project_scan'])
        self.edit('TD/unused.tex')
        self.assertEqual(self.run_sync()['compiled'], ['TD/td1.tex'])

    def test_missing_cached_pdf_and_force_trigger_rebuilds(self):
        self.run_sync()
        (self.directory / 'cache/project-id/TD/td1.pdf').unlink()
        self.assertEqual(self.run_sync()['compiled'], ['TD/td1.tex'])
        self.assertEqual(self.run_sync(force=True)['compiled'], self.roots)

    def test_sources_and_pdfs_share_project_folders_with_metadata_only_in_state(self):
        self.client.uploads['TD/logo.png'] = b'image'
        self.client.texts['TD/preamble.tex'] += r'\includegraphics{logo}'
        self.client.artifacts = {'output.fls': b'PWD /compile/TD\nINPUT preamble.tex\n'}
        report = self.run_sync()
        cache = self.directory / 'cache/project-id'
        self.assertEqual((cache / 'TD/td1.tex').read_text(), self.client.texts['TD/td1.tex'])
        self.assertEqual((cache / 'TD/preamble.tex').read_text(), self.client.texts['TD/preamble.tex'])
        self.assertEqual((cache / 'TD/logo.png').read_bytes(), b'image')
        self.assertTrue((cache / 'TD/td1.pdf').is_file())
        self.assertFalse((cache / 'sources').exists())
        self.assertFalse((cache / 'dependencies').exists())
        self.assertFalse((cache / 'manifest.json').exists())
        self.assertEqual(list(cache.rglob('*.fls')), [])
        state = json.loads((self.directory / 'state.json').read_text())
        self.assertEqual(state['version'], 2)
        self.assertEqual(state['projects']['https://latex.example/project/project-id']['builds']['TD/td1.tex'], report['files'][0])
        self.assertEqual(self.run_sync()['compiled'], [])

    def test_uploaded_pdf_with_compiled_name_stays_distinct_and_changes_are_detected(self):
        self.client.uploads['TD/td1.pdf'] = b'%PDF-uploaded-source'
        self.client.uploads['TD/td1.source.pdf'] = b'%PDF-another-input'
        self.client.texts['TD/td1.tex'] += r'\includegraphics{td1.pdf}\includegraphics{td1.source.pdf}'
        report = self.run_sync()
        cache = self.directory / 'cache/project-id/TD'
        self.assertEqual((cache / 'td1.source-2.pdf').read_bytes(), b'%PDF-uploaded-source')
        self.assertEqual((cache / 'td1.source.pdf').read_bytes(), b'%PDF-another-input')
        self.assertNotEqual((cache / 'td1.pdf').read_bytes(), b'%PDF-uploaded-source')
        self.assertEqual(report['files'][0]['inputs']['TD/td1.pdf']['cache_path'], 'TD/td1.source-2.pdf')
        self.assertEqual(self.run_sync()['compiled'], [])
        self.client.uploads['TD/td1.pdf'] = b'%PDF-updated-source'
        self.assertEqual(self.run_sync()['compiled'], ['TD/td1.tex'])
        old_pdf = (cache / 'td1.pdf').read_bytes()
        self.client.uploads['TD/td1.pdf'] = b'%PDF-next-source'
        self.client.fail = True
        with self.assertRaises(CompilationError):
            self.run_sync()
        self.assertEqual((cache / 'td1.pdf').read_bytes(), old_pdf)

    def test_project_directory_cannot_be_overwritten_by_compiled_pdf(self):
        with self.assertRaisesRegex(CompilationError, 'directory conflicts'):
            source_locations({'notes.tex': {}, 'notes.pdf/figure.png': {}})

    def test_moved_cache_reuses_valid_files_and_checks_output_integrity(self):
        self.run_sync()
        state_path = self.directory / 'state.json'
        relocated = self.directory / 'moved-cache'
        (self.directory / 'cache').rename(relocated)
        self.client.compiled.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            report = compile_documents(self.client, 'project-id', self.roots, relocated, 30, state_file=state_path)
        self.assertEqual(report['skipped'], self.roots)
        self.assertEqual(self.client.compiled, [])
        # Relocation must not let modified local content bypass hash checks.
        (relocated / 'project-id/TD/td1.pdf').write_bytes(b'%PDF-modified')
        with contextlib.redirect_stdout(io.StringIO()):
            report = compile_documents(self.client, 'project-id', self.roots, relocated, 30, state_file=state_path)
        self.assertEqual(report['compiled'], ['TD/td1.tex'])
        self.assertEqual(report['skipped'], ['TD/td2.tex'])

    def test_actual_compiler_settings_still_invalidate_cached_pdfs(self):
        self.run_sync()
        original_tree = self.client.tree
        for settings in ({'compiler': 'xelatex'}, {'imageName': 'another-tex-environment'}):
            with self.subTest(settings=settings), patch.object(self.client, 'tree', side_effect=lambda: {**original_tree(), **settings}):
                self.assertEqual(self.run_sync()['compiled'], self.roots)
                self.assertEqual(self.run_sync()['compiled'], [])


class RevisionTests(unittest.TestCase):
    def socket_with_replies(self, replies):
        socket = Mock()
        pending = iter(replies)
        def emit(event, *args):
            if event == 'joinDoc':
                reply = next(pending)
                if reply is not None:
                    args[-1](*reply)
        socket.emit.side_effect = emit
        return socket

    def test_missing_operation_history_fetches_full_source_once(self):
        socket = self.socket_with_replies([
            ({'message': 'doc updater could not load requested ops'},),
            (None, ['First line', 'Second line'], 4),
        ])
        self.assertEqual(PLMlatexClient.read_document(socket, 'id', 3),
                         {'version': 4, 'text': 'First line\nSecond line'})
        calls = socket.emit.call_args_list
        self.assertEqual([call.args[0] for call in calls], ['joinDoc', 'leaveDoc', 'joinDoc', 'leaveDoc'])
        self.assertEqual([call.args[2] for call in calls if call.args[0] == 'joinDoc'], [3, -1])

    def test_server_errors_have_bounded_retries_and_do_not_expose_response(self):
        for previous, expected in ((3, [3, -1]), (-1, [-1])):
            with self.subTest(previous=previous):
                socket = self.socket_with_replies([({'message': 'secret response'},)] * 2)
                with self.assertRaisesRegex(CompilationError, 'rejected the full source request') as error:
                    PLMlatexClient.read_document(socket, 'id', previous)
                self.assertNotIn('secret', str(error.exception))
                self.assertEqual([call.args[2] for call in socket.emit.call_args_list if call.args[0] == 'joinDoc'], expected)

    def test_timeout_is_distinct_and_does_not_start_another_request(self):
        socket = self.socket_with_replies([None])
        with self.assertRaisesRegex(CompilationError, 'Timed out'):
            PLMlatexClient.read_document(socket, 'id', 3, timeout=7)
        socket.wait_for_callbacks.assert_called_once_with(seconds=7)
        self.assertEqual(sum(call.args[0] == 'joinDoc' for call in socket.emit.call_args_list), 1)

    def test_malformed_replies_are_not_treated_as_missing_history(self):
        for reply in ((), (None,), (None, ['source'])):
            with self.subTest(reply=reply):
                socket = self.socket_with_replies([reply])
                with self.assertRaisesRegex(CompilationError, 'invalid document response'):
                    PLMlatexClient.read_document(socket, 'id', 3)
                self.assertEqual(sum(call.args[0] == 'joinDoc' for call in socket.emit.call_args_list), 1)

    def test_invalid_revision_is_rejected(self):
        for version in (None, '4', True, -1):
            with self.subTest(version=version):
                socket = self.socket_with_replies([(None, ['source'], version)])
                with self.assertRaisesRegex(CompilationError, 'invalid revision'):
                    PLMlatexClient.read_document(socket, 'id', -1)

    def test_full_snapshot_must_contain_source_even_after_fallback(self):
        for previous in (-1, 3):
            with self.subTest(previous=previous):
                replies = [({'message': 'missing history'},)] if previous != -1 else []
                socket = self.socket_with_replies([*replies, (None, None, 4)])
                with self.assertRaisesRegex(CompilationError, 'no source text'):
                    PLMlatexClient.read_document(socket, 'id', previous)

    def test_changed_revision_fetches_full_source_after_incremental_reply(self):
        socket = Mock()
        responses = [(None, None, 2), (None, ['cafÃ©'], 2)]
        def emit(event, *args):
            if event == 'joinDoc':
                args[-1](*responses.pop(0))
        socket.emit.side_effect = emit
        result = PLMlatexClient.read_document(socket, 'id', 0)
        self.assertEqual(result, {'version': 2, 'text': 'café'})

    def test_unchanged_revision_does_not_request_full_source(self):
        socket = Mock()
        def emit(event, *args):
            if event == 'joinDoc':
                args[-1](None, None, 2)
        socket.emit.side_effect = emit
        self.assertEqual(PLMlatexClient.read_document(socket, 'id', 2), {'version': 2, 'text': None})
        self.assertEqual(sum(call.args[0] == 'joinDoc' for call in socket.emit.call_args_list), 1)
