"""Configuration behavior, validation, and CLI integration without remote writes."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from plm_moodle_sync.cli import main
from plm_moodle_sync.config import ConfigError, load_config
from test_dependencies import FakeProject


PROJECT_A = '0123456789abcdef01234567'
PROJECT_B = 'fedcba9876543210fedcba98'
FULL = {
    'plmlatex': {'server': 'https://latex.example/', 'cookie_file': 'auth/plmlatex.json', 'project_id': PROJECT_A},
    'moodle': {'server': 'https://moodle.example', 'course_id': 123},
    'files': [
        {'source': {'tex': 'TD/td1.tex'},
         'target': {'section': 'Travaux dirigés', 'name': 'TD1', 'filename': 'Introduction.pdf'}},
        {'source': {'tex': 'TD/td2.tex'},
         'target': {'section': 'Études qualitatives', 'name': 'TD2', 'filename': 'TD2.pdf'}},
    ],
}


@contextlib.contextmanager
def in_directory(directory):
    previous = Path.cwd()
    os.chdir(directory)
    try:
        yield
    finally:
        os.chdir(previous)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / 'config' / 'sync.yaml'
        self.path.parent.mkdir()

    def write(self, data):
        self.path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding='utf-8')
        return self.path

    def test_single_project_course_and_unicode_targets(self):
        config = load_config(self.write(FULL))
        self.assertEqual(config.plmlatex.server, 'https://latex.example')
        self.assertEqual(config.plmlatex.project_id, PROJECT_A)
        self.assertEqual(config.moodle.course_id, 123)
        for file in config.files:
            self.assertFalse(hasattr(file.source, 'project_id'))
            self.assertFalse(hasattr(file.target, 'course_id'))
        self.assertEqual(config.files[1].target.section, 'Études qualitatives')
        self.assertEqual(config.files[1].target.filename, 'TD2.pdf')

    def test_optional_sync_and_each_omitted_field_use_config_directory(self):
        for sync in (None, {}, {'state_file': 'status.json'}, {'cache_dir': '../pdfs'}, {'log_file': '../logs/run.log'}):
            with self.subTest(sync=sync):
                data = copy.deepcopy(FULL)
                if sync is not None:
                    data['sync'] = sync
                self.write(data)
                with in_directory(self.directory):
                    config = load_config('config/sync.yaml')
                self.assertEqual(config.sync.state_file, (self.path.parent / (sync or {}).get('state_file', '.cache/sync.json')).resolve())
                self.assertEqual(config.sync.cache_dir, (self.path.parent / (sync or {}).get('cache_dir', '.cache/files')).resolve())
                self.assertEqual(config.sync.log_file, (self.path.parent / (sync or {}).get('log_file', '.cache/sync.log')).resolve())
                self.assertEqual(config.plmlatex.cookie_file, self.path.parent / 'auth/plmlatex.json')
                self.assertFalse(config.sync.state_file.exists())
                self.assertFalse(config.sync.cache_dir.exists())
                self.assertFalse(config.sync.log_file.exists())

    def test_display_name_is_independent_of_pdf_filename_override(self):
        data = copy.deepcopy(FULL)
        data['files'][0]['target']['name'] = 'TD1 — Introduction'
        config = load_config(self.write(data))
        self.assertEqual(config.files[0].target.name, 'TD1 — Introduction')
        self.assertEqual(config.files[0].target.filename, 'Introduction.pdf')
        self.assertEqual(config.files[1].target.name, 'TD2')

    def test_duplicate_display_names_in_one_section_fail_before_network_access(self):
        for name in ('TD1', '<b>TD1</b>', ' TD1 '):
            with self.subTest(name=name):
                data = copy.deepcopy(FULL)
                data['files'][1]['target'].update(section='Travaux dirigés', name=name)
                self.write(data)
                with patch('plm_moodle_sync.cli.run_sync') as run, contextlib.redirect_stderr(io.StringIO()) as stderr:
                    self.assertEqual(main(['sync', '--config', str(self.path)]), 1)
                run.assert_not_called()
                self.assertIn('duplicates another Moodle display name', stderr.getvalue())

    def test_display_name_must_have_visible_text(self):
        data = copy.deepcopy(FULL)
        data['files'][0]['target']['name'] = '<span> </span>'
        with self.assertRaisesRegex(ConfigError, 'must contain visible text'):
            load_config(self.write(data))

    def test_section_preserves_integer_id_and_quoted_numeric_name(self):
        for value in (20182, '20182', 'Test'):
            with self.subTest(section=value):
                data = copy.deepcopy(FULL)
                data['files'][0]['target']['section'] = value
                target = load_config(self.write(data)).files[0].target
                self.assertEqual(target.section, value)
                self.assertIs(type(target.section), type(value))

    def test_visible_is_optional_and_accepts_only_booleans(self):
        self.assertIsNone(load_config(self.write(FULL)).files[0].target.visible)
        for value in (True, False):
            data = copy.deepcopy(FULL)
            data['files'][0]['target']['visible'] = value
            self.assertIs(load_config(self.write(data)).files[0].target.visible, value)
        for value in (None, 0, 1, 1.0, 'true', 'false', '', [], {}):
            with self.subTest(value=value):
                data = copy.deepcopy(FULL)
                data['files'][0]['target']['visible'] = value
                with self.assertRaisesRegex(ConfigError, 'target.visible must be true or false'):
                    load_config(self.write(data))

    def test_old_section_keys_are_rejected(self):
        for key, value in [('section_name', 'Test'), ('section_id', 20182)]:
            with self.subTest(key=key):
                data = copy.deepcopy(FULL)
                destination = data['files'][0]['target']
                del destination['section']
                destination[key] = value
                with self.assertRaisesRegex(ConfigError, 'unknown option'):
                    load_config(self.write(data))

    def test_pages_accepts_a_quoted_page_or_inclusive_range_and_can_be_omitted(self):
        self.assertIsNone(load_config(self.write(FULL)).files[0].target.pages)
        for value, expected in [('1-11', '1-11'), ('3', '3'), ('3-3', '3'), (' 2-5 ', '2-5')]:
            with self.subTest(value=value):
                data = copy.deepcopy(FULL)
                data['files'][0]['target']['pages'] = value
                self.assertEqual(load_config(self.write(data)).files[0].target.pages, expected)

    def test_invalid_pages_are_rejected_before_fetching(self):
        for value in (None, True, False, 1, 1.5, [], {}, '', '0', '0-2', '11-1', '-1',
                      '1-', '1,3', '1-2-3', 'all', '1:11', '1.0', '01-11'):
            with self.subTest(value=value):
                data = copy.deepcopy(FULL)
                data['files'][0]['target']['pages'] = value
                with self.assertRaisesRegex(ConfigError, 'target.pages'):
                    load_config(self.write(data))

    def test_multiple_chapters_can_share_one_source_with_distinct_destinations(self):
        data = copy.deepcopy(FULL)
        data['files'][0]['target']['pages'] = '1-11'
        chapter = copy.deepcopy(data['files'][0])
        chapter['target'].update(name='Chapitre 2', filename='chapitre2.pdf', pages='12-20')
        data['files'].append(chapter)
        config = load_config(self.write(data))
        self.assertEqual(config.files[0].source, config.files[2].source)
        self.assertEqual(config.files[2].target.pages, '12-20')

    def test_omitted_filename_uses_tex_basename_and_preserves_required_name(self):
        for tex, expected in [('TD/TD1-Introduction.tex', 'TD1-Introduction.pdf'),
                              ('nested/folder/Équations.v2.TeX', 'Équations.v2.pdf'),
                              ('notes.tex', 'notes.pdf')]:
            with self.subTest(tex=tex):
                data = copy.deepcopy(FULL)
                data['files'][0]['source']['tex'] = tex
                del data['files'][0]['target']['filename']
                target = load_config(self.write(data)).files[0].target
                self.assertEqual(target.filename, expected)
                self.assertEqual(target.name, 'TD1')

    def test_inferred_filename_conflicts_with_explicit_same_destination(self):
        data = copy.deepcopy(FULL)
        del data['files'][0]['target']['filename']
        duplicate = copy.deepcopy(data['files'][0])
        duplicate['source']['tex'] = 'other.tex'
        duplicate['target'].update(name='Another name', filename='td1.pdf')
        data['files'].append(duplicate)
        with self.assertRaisesRegex(ConfigError, 'duplicates another Moodle destination'):
            load_config(self.write(data))

    def test_different_names_do_not_allow_duplicate_pdf_destinations(self):
        data = copy.deepcopy(FULL)
        duplicate = copy.deepcopy(data['files'][0])
        duplicate['target']['name'] = 'Another display name'
        data['files'].append(duplicate)
        with self.assertRaisesRegex(ConfigError, 'duplicates another Moodle destination'):
            load_config(self.write(data))

    def test_absolute_paths_are_preserved(self):
        data = copy.deepcopy(FULL)
        data['sync'] = {'cache_dir': str(self.directory / 'pdfs'), 'state_file': str(self.directory / 'status.json')}
        config = load_config(self.write(data))
        self.assertEqual(config.sync.cache_dir, self.directory / 'pdfs')
        self.assertEqual(config.sync.state_file, self.directory / 'status.json')

    def test_old_defaults_and_per_file_identifiers_are_rejected(self):
        for field, value in [('defaults', {'project_id': PROJECT_A, 'course_id': 123}),
                             ('source', PROJECT_A), ('source', PROJECT_B), ('target', 123), ('target', 456)]:
            with self.subTest(field=field, value=value):
                data = copy.deepcopy(FULL)
                if field == 'defaults':
                    data[field] = value
                else:
                    data['files'][0][field]['project_id' if field == 'source' else 'course_id'] = value
                with self.assertRaisesRegex(ConfigError, 'unknown option'):
                    load_config(self.write(data))

    def test_fetch_only_and_login_only_configs_do_not_need_moodle_credentials(self):
        self.assertEqual(load_config(self.write({'plmlatex': {'cookie_file': '.secrets/plmlatex.json'}})).files, ())
        data = {'plmlatex': {'project_id': PROJECT_A}, 'files': [{'source': {'tex': 'TD/td1.tex'}}]}
        with patch.dict(os.environ, {}, clear=True):
            config = load_config(self.write(data))
        self.assertIsNone(config.files[0].target)
        self.assertEqual(config.plmlatex.cookie_file, self.path.parent / '.secrets/plmlatex.json')

    def test_malformed_types_ids_paths_and_unknown_fields_are_rejected(self):
        for location, value in [
            (('moodle', 'course_id'), True), (('moodle', 'course_id'), 0),
            (('plmlatex', 'project_id'), 'not-an-id'), (('moodle', 'course_id'), '123'),
            (('plmlatex', 'project_id'), None), (('moodle', 'course_id'), None),
            (('files', 0, 'source', 'tex'), '../outside.tex'),
            (('files', 0, 'target', 'filename'), '../file.pdf'),
            (('files', 0, 'target', 'filename'), 'file.txt'),
            (('files', 0, 'target', 'filename'), None),
            (('files', 0, 'target', 'filename'), ''),
            (('files', 0, 'target', 'name'), ''),
            (('files', 0, 'target', 'name'), None),
            (('files', 0, 'target', 'name'), 123),
            (('files', 0, 'target', 'section_id'), 7),
            (('files', 0, 'target', 'section'), '  '),
            (('files', 0, 'target', 'section'), 0),
            (('files', 0, 'target', 'section'), -1),
            (('files', 0, 'target', 'section'), True),
            (('files', 0, 'target', 'section'), False),
            (('files', 0, 'target', 'section'), 20182.0),
            (('files', 0, 'target', 'section'), None),
            (('files', 0, 'target', 'section'), []),
            (('files', 0, 'target'), None), (('files',), {}),
            (('sync',), None), (('sync',), {'cache_dri': 'typo'}),
            (('plmlatex', 'server'), 'http://latex.example'),
            (('moodle', 'server'), 'https://user:secret@moodle.example'),
            (('moodle', 'token'), 'secret'), (('moodle', 'token_env'), '${TOKEN}'),
        ]:
            with self.subTest(location=location, value=value):
                data = copy.deepcopy(FULL)
                container = data
                for key in location[:-1]:
                    container = container[key]
                container[location[-1]] = value
                with self.assertRaises(ConfigError):
                    load_config(self.write(data))

    def test_missing_required_mapping_fields_are_rejected(self):
        for location in [('plmlatex', 'project_id'), ('moodle', 'course_id'),
                         ('files', 0, 'source'), ('files', 0, 'source', 'tex'),
                         ('files', 0, 'target', 'section'), ('files', 0, 'target', 'name')]:
            with self.subTest(location=location):
                data = copy.deepcopy(FULL)
                container = data
                for key in location[:-1]:
                    container = container[key]
                del container[location[-1]]
                with self.assertRaises(ConfigError):
                    load_config(self.write(data))

    def test_duplicate_keys_unsafe_tags_and_syntax_errors_are_rejected(self):
        for content in ('defaults: {}\ndefaults: {}', '!!python/object/apply:os.system ["false"]',
                        'moodle: [hidden-secret', '[]', ''):
            self.path.write_text(content)
            with self.subTest(content=content), self.assertRaises(ConfigError) as error:
                load_config(self.path)
            self.assertNotIn('hidden-secret', str(error.exception))

    def test_duplicate_destinations_and_colliding_local_pdfs_are_rejected(self):
        data = copy.deepcopy(FULL)
        data['files'].append(copy.deepcopy(data['files'][0]))
        with self.assertRaisesRegex(ConfigError, 'duplicates another Moodle destination'):
            load_config(self.write(data))
        data = copy.deepcopy(FULL)
        data['files'].append({'source': {'tex': 'TD/td1.TeX'}})
        with self.assertRaisesRegex(ConfigError, 'overwrite another document PDF'):
            load_config(self.write(data))

    def test_state_file_cannot_overwrite_session(self):
        data = copy.deepcopy(FULL)
        data['sync'] = {'state_file': 'auth/plmlatex.json'}
        with self.assertRaisesRegex(ConfigError, 'different files'):
            load_config(self.write(data))


class ConfigCLITests(unittest.TestCase):
    setUp = ConfigTests.setUp
    write = ConfigTests.write

    def test_login_uses_yaml_server_and_cookie_path_from_another_directory(self):
        self.write(FULL)
        with in_directory(self.directory), patch('plm_moodle_sync.cli.login') as login, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['plmlatex-login', '--config', 'config/sync.yaml']), 0)
        login.assert_called_once_with(self.path.parent / 'auth/plmlatex.json', server='https://latex.example', timeout=30)

    def test_cli_options_override_yaml_and_cli_paths_stay_relative_to_cwd(self):
        self.write(FULL)
        with patch('plm_moodle_sync.cli.login') as login, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['plmlatex-login', '--config', str(self.path), '--server', 'https://override.example',
                                   '--cookie-file', 'override-session.json', '--timeout', '8']), 0)
        login.assert_called_once_with(Path('override-session.json'), server='https://override.example', timeout=8)
        with patch('plm_moodle_sync.sync.load_session', return_value={'sid': 'test'}), \
             patch('plm_moodle_sync.sync.PLMlatexClient'), patch('plm_moodle_sync.sync.compile_documents') as compile_call, \
             patch('plm_moodle_sync.cli.state_lock'), \
             contextlib.redirect_stdout(io.StringIO()):
            compile_call.return_value = {'compiled': [], 'skipped': []}
            self.assertEqual(main(['fetch', '--config', str(self.path), '--output-dir', 'override-cache',
                                   '--state-file', 'override-state.json', '--force', '--timeout', '9']), 0)
        self.assertEqual(compile_call.call_args.args[3:5], (Path('override-cache'), 9))
        self.assertEqual(compile_call.call_args.kwargs, {'state_file': Path('override-state.json'), 'force': True})

    def test_config_fetch_deduplicates_source_used_by_two_targets(self):
        data = copy.deepcopy(FULL)
        another = copy.deepcopy(data['files'][0])
        another['target']['filename'] = 'Another-copy.pdf'
        another['target']['name'] = 'Another copy'
        data['files'].append(another)
        self.write(data)
        with patch('plm_moodle_sync.sync.load_session', return_value={'sid': 'test'}) as load, \
             patch('plm_moodle_sync.sync.PLMlatexClient') as client, \
             patch('plm_moodle_sync.sync.compile_documents') as compile_call, \
             contextlib.redirect_stdout(io.StringIO()):
            compile_call.return_value = {'compiled': [], 'skipped': []}
            self.assertEqual(main(['fetch', '--config', str(self.path)]), 0)
        load.assert_called_once_with(self.path.parent / 'auth/plmlatex.json', server='https://latex.example')
        client.assert_called_once_with(cookie={'sid': 'test'}, base_url='https://latex.example')
        self.assertEqual([call.args[1:3] for call in compile_call.call_args_list],
                         [(PROJECT_A, ['TD/td1.tex', 'TD/td2.tex'])])
        self.assertTrue(all(call.args[3] == self.path.parent / '.cache/files' for call in compile_call.call_args_list))

    def test_filters_fetch_only_selected_source_in_the_configured_project(self):
        self.write(FULL)
        with patch('plm_moodle_sync.sync.load_session', return_value={'sid': 'test'}), \
             patch('plm_moodle_sync.sync.compile_documents') as compile_call:
            self.assertEqual(main(['fetch', '--config', str(self.path),
                                   '--tex', 'TD/td2.tex']), 0)
        compile_call.assert_called_once()
        self.assertEqual(compile_call.call_args.args[1:3], (PROJECT_A, ['TD/td2.tex']))

    def test_project_cli_selectors_are_only_for_use_without_yaml(self):
        self.write(FULL)
        for option, value in [('--project-id', PROJECT_B), ('--project-name', 'test')]:
            with patch('plm_moodle_sync.sync.load_session') as load, contextlib.redirect_stderr(io.StringIO()), \
                 self.assertRaises(SystemExit) as error:
                main(['fetch', '--config', str(self.path), option, value])
            self.assertEqual(error.exception.code, 2)
            load.assert_not_called()

    def test_bad_selection_and_empty_config_stop_before_authentication(self):
        for data, flags in [(FULL, ['--tex', 'missing.tex']), ({}, []),
                            ({'plmlatex': {'project_id': PROJECT_A}}, [])]:
            with self.subTest(flags=flags):
                self.write(data)
                with patch('plm_moodle_sync.sync.load_session') as load, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(['fetch', '--config', str(self.path), *flags]), 1)
                load.assert_not_called()

    def test_invalid_later_mapping_stops_before_any_network_work(self):
        data = copy.deepcopy(FULL)
        data['files'][1]['target']['section_id'] = 3
        self.write(data)
        with patch('plm_moodle_sync.sync.load_session') as load, patch('plm_moodle_sync.sync.compile_documents') as compile_call, \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(['fetch', '--config', str(self.path)]), 1)
        load.assert_not_called()
        compile_call.assert_not_called()
        self.assertIn('files[2].target', stderr.getvalue())

    def test_listing_uses_configured_project_without_file_mappings(self):
        self.write({'plmlatex': {'project_id': PROJECT_A}})
        with patch('plm_moodle_sync.sync.load_session', return_value={'sid': 'test'}), \
             patch('plm_moodle_sync.sync.PLMlatexClient') as client, patch('plm_moodle_sync.sync.compile_documents') as compile_call, \
             contextlib.redirect_stdout(io.StringIO()) as stdout:
            client.return_value.document_paths.return_value = {'TD/td1.tex': 'id'}
            self.assertEqual(main(['fetch', '--config', str(self.path), '--list-documents']), 0)
        compile_call.assert_not_called()
        client.return_value.refresh_csrf.assert_called_once_with(PROJECT_A)
        self.assertIn('TD/td1.tex', stdout.getvalue())

    def test_multiple_yaml_fetch_preserves_shared_state_and_skips_unchanged_rerun(self):
        first = copy.deepcopy(FULL)
        first['files'] = first['files'][:1]
        self.write(first)
        second = copy.deepcopy(FULL)
        second['plmlatex']['project_id'] = PROJECT_B
        second['moodle']['course_id'] = 456
        second['files'] = second['files'][1:]
        other_path = self.path.with_name('second.yaml')
        other_path.write_text(yaml.safe_dump(second), encoding='utf-8')
        client = FakeProject()
        with patch('plm_moodle_sync.sync.load_session', return_value={'sid': 'test'}), \
             patch('plm_moodle_sync.sync.PLMlatexClient', return_value=client), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['fetch', '--config', str(self.path), str(other_path)]), 0)
            self.assertEqual(client.compiled, ['TD/td1.tex', 'TD/td2.tex'])
            client.compiled.clear()
            self.assertEqual(main(['fetch', '--config', str(self.path), str(other_path)]), 0)
            self.assertEqual(client.compiled, [])
        state = json.loads((self.path.parent / '.cache/sync.json').read_text())
        self.assertEqual(set(state['projects']), {'https://latex.example/project/' + PROJECT_A,
                                                 'https://latex.example/project/' + PROJECT_B})
        self.assertTrue((self.path.parent / '.cache/files' / PROJECT_A / 'TD/td1.pdf').is_file())
        self.assertTrue((self.path.parent / '.cache/files' / PROJECT_B / 'TD/td2.pdf').is_file())


if __name__ == '__main__':
    unittest.main()
