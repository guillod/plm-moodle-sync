"""Multiple YAML jobs: settings isolation, preflight, logs, and failure handling."""

import contextlib
from copy import deepcopy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from plm_moodle_sync.cli import main
from plm_moodle_sync.moodle.errors import MoodleError
from test_config import FULL, PROJECT_A, PROJECT_B


class BatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.first = self.root / 'first/sync.yaml'
        self.second = self.root / 'second/sync.yaml'
        self.other = deepcopy(FULL)
        self.other['plmlatex'].update(server='https://other-latex.example', project_id=PROJECT_B)
        self.other['moodle'].update(server='https://other-moodle.example', course_id=456)
        self.write(self.first, FULL)
        self.write(self.second, self.other)

    def write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data), encoding='utf-8')

    def command(self, command='sync', *flags):
        with contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            status = main([command, '--config', str(self.first), str(self.second), *flags])
        return status, stdout.getvalue(), stderr.getvalue()

    def test_fetch_isolates_servers_sessions_project_ids_and_relative_paths(self):
        with patch('plm_moodle_sync.sync.load_session', return_value={}) as auth, \
             patch('plm_moodle_sync.sync.PLMlatexClient') as client, \
             patch('plm_moodle_sync.sync.compile_documents') as compile_call:
            self.assertEqual(self.command('fetch')[0], 0)
        self.assertEqual([call.kwargs['server'] for call in auth.call_args_list],
                         ['https://latex.example', 'https://other-latex.example'])
        self.assertEqual([call.args[0] for call in auth.call_args_list],
                         [path.parent / 'auth/plmlatex.json' for path in (self.first, self.second)])
        self.assertEqual([call.kwargs['base_url'] for call in client.call_args_list],
                         ['https://latex.example', 'https://other-latex.example'])
        self.assertEqual([call.args[1] for call in compile_call.call_args_list], [PROJECT_A, PROJECT_B])
        for call, path in zip(compile_call.call_args_list, (self.first, self.second)):
            self.assertEqual(call.args[3], path.parent / '.cache/files')
            self.assertEqual(call.kwargs['state_file'], path.parent / '.cache/sync.json')
            log = (path.parent / '.cache/sync.log').read_text()
            self.assertIn('Configuration: ' + str(path), log)
            self.assertIn('status=success exit_code=0', log)

    def test_explicit_cli_overrides_apply_to_every_job(self):
        with patch('plm_moodle_sync.cli.login') as login:
            self.assertEqual(self.command('plmlatex-login', '--server', 'https://override.example',
                                          '--cookie-file', str(self.root / 'session.json'))[0], 0)
        self.assertEqual(login.call_count, 2)
        for call in login.call_args_list:
            self.assertEqual(call.args, (self.root / 'session.json',))
            self.assertEqual(call.kwargs, {'server': 'https://override.example', 'timeout': 30})

    def test_repeated_config_options_preserve_order_and_skip_duplicate_paths(self):
        with patch('plm_moodle_sync.cli.run_sync') as run, contextlib.redirect_stdout(io.StringIO()):
            status = main(['upload', '--config', str(self.first), '--config', str(self.second), str(self.first), '--dry-run'])
        self.assertEqual(status, 0)
        self.assertEqual([call.args[1].path for call in run.call_args_list], [self.first, self.second])
        self.assertEqual([call.args[1].moodle.course_id for call in run.call_args_list], [123, 456])
        for call in run.call_args_list:
            self.assertTrue(call.args[0].dry_run)

    def test_invalid_later_yaml_stops_all_work_before_logs_or_network(self):
        self.other['files'][0]['target']['course_id'] = 789
        self.write(self.second, self.other)
        with patch('plm_moodle_sync.cli.run_sync') as run:
            status, _, stderr = self.command()
        self.assertEqual(status, 1)
        self.assertIn(str(self.second), stderr)
        self.assertIn('course_id', stderr)
        run.assert_not_called()
        self.assertFalse((self.first.parent / '.cache').exists())
        self.assertFalse((self.second.parent / '.cache').exists())

    def test_invalid_later_selection_stops_fetch_batch_before_authentication(self):
        self.other['files'] = self.other['files'][1:]
        self.write(self.second, self.other)
        with patch('plm_moodle_sync.sync.load_session') as auth:
            self.assertEqual(self.command('fetch', '--tex', 'TD/td1.tex')[0], 1)
        auth.assert_not_called()
        self.assertFalse((self.first.parent / '.cache').exists())

    def test_later_log_cannot_overwrite_another_config_or_its_cache(self):
        for path in (self.first, self.first.parent / '.cache/sync.json',
                     self.first.parent / 'auth/plmlatex.json', self.first.parent / '.cache/files/extra.log'):
            with self.subTest(path=path):
                self.other['sync'] = {'log_file': str(path)}
                self.write(self.second, self.other)
                before = self.first.read_bytes()
                with patch('plm_moodle_sync.cli.run_sync') as run:
                    self.assertEqual(self.command()[0], 1)
                run.assert_not_called()
                self.assertEqual(self.first.read_bytes(), before)
                self.assertFalse((self.first.parent / '.cache').exists())

    def test_runtime_failure_continues_remaining_jobs_and_returns_failure(self):
        with patch('plm_moodle_sync.cli.run_sync', side_effect=[MoodleError('Session expired'), None]) as run:
            status, _, stderr = self.command()
        self.assertEqual(status, 1)
        self.assertEqual(run.call_count, 2)
        self.assertIn('Session expired', stderr)
        self.assertIn('status=failed exit_code=1', (self.first.parent / '.cache/sync.log').read_text())
        self.assertIn('status=success exit_code=0', (self.second.parent / '.cache/sync.log').read_text())

    def test_interrupt_stops_remaining_jobs(self):
        with patch('plm_moodle_sync.cli.run_sync', side_effect=KeyboardInterrupt) as run:
            self.assertEqual(self.command()[0], 130)
        run.assert_called_once()
        self.assertFalse((self.second.parent / '.cache').exists())

    def test_moodle_check_uses_each_configured_course_and_session(self):
        with patch('plm_moodle_sync.sync.moodle_auth.load_cookies', return_value={}) as auth, \
             patch('plm_moodle_sync.sync.MoodleClient') as client:
            check = client.return_value.__enter__.return_value.check
            check.return_value = {'sections': []}
            self.assertEqual(self.command('moodle-check', '--section-name', 'Test')[0], 0)
        self.assertEqual([call.args[0] for call in check.call_args_list], [123, 456])
        self.assertEqual([call.args for call in auth.call_args_list],
                         [(self.first.parent / '.secrets/moodle.json', 'https://moodle.example'),
                          (self.second.parent / '.secrets/moodle.json', 'https://other-moodle.example')])
