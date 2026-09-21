"""Persistent logs, bounded rotation, failure handling, and credential isolation."""

import contextlib
from copy import deepcopy
import io
import logging
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
import yaml

from plm_moodle_sync.cli import main
from plm_moodle_sync.common.runlog import log_event, log_run, report
from plm_moodle_sync.config import load_config
from plm_moodle_sync.moodle.errors import MoodleError
from test_config import FULL, in_directory
from test_dependencies import FakeProject


class RunLogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config_path = self.root / 'config/sync.yaml'
        self.config_path.parent.mkdir()
        self.data = deepcopy(FULL)
        self.write_config()
        self.log = self.config_path.parent / '.cache/sync.log'

    def write_config(self):
        self.config_path.write_text(yaml.safe_dump(self.data), encoding='utf-8')

    def command(self, command='upload', *flags):
        with contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = main([command, '--config', str(self.config_path), *flags])
        return result, stdout.getvalue(), stderr.getvalue()

    def test_actual_fetch_records_compile_skip_and_separate_run_outcomes(self):
        self.data['files'] = [self.data['files'][0]]
        self.write_config()
        client = FakeProject()
        with patch('plm_moodle_sync.sync.load_session', return_value={'sid': 'cookie-secret'}), \
             patch('plm_moodle_sync.sync.PLMlatexClient', return_value=client):
            self.assertEqual(self.command('fetch')[0], 0)
            self.assertEqual(self.command('fetch')[0], 0)
        content = self.log.read_text()
        self.assertIn('Compiling TD/td1.tex', content)
        self.assertIn('1 compiled, 0 unchanged', content)
        self.assertIn('Unchanged TD/td1.tex; using cached PDF.', content)
        self.assertIn('0 compiled, 1 unchanged', content)
        self.assertEqual(content.count('status=success exit_code=0'), 2)
        self.assertEqual(len(set(re.findall(r'run=([a-f0-9]+)', content))), 2)
        self.assertRegex(content, r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4} INFO run=')
        self.assertIn('duration_seconds=', content)
        self.assertNotIn('cookie-secret', content)
        self.assertEqual(self.log.stat().st_mode & 0o777, 0o600)

    def test_failure_keeps_prior_progress_and_terminal_output(self):
        def run(*_):
            report('create: TD1.pdf (resource 77)')
            raise MoodleError('TD2 upload failed')

        with patch('plm_moodle_sync.cli.run_sync', side_effect=run):
            result, stdout, stderr = self.command()
        self.assertEqual(result, 1)
        self.assertIn('create: TD1.pdf', stdout)
        self.assertIn('TD2 upload failed', stderr)
        content = self.log.read_text()
        self.assertIn('create: TD1.pdf (resource 77)', content)
        self.assertIn('Error: TD2 upload failed', content)
        self.assertIn('status=failed exit_code=1', content)

    def test_network_and_unexpected_errors_never_persist_raw_exception_text(self):
        for exception, expected in [(requests.Timeout('https://host/?token=privateSecret'), 'network request failed'),
                                    (RuntimeError('Cookie: privateSecret'), 'Unexpected runtime failure (RuntimeError)')]:
            with self.subTest(exception=type(exception)), patch('plm_moodle_sync.cli.run_sync', side_effect=exception):
                result, _, stderr = self.command()
                self.assertEqual(result, 1)
                self.assertIn(expected, stderr)
                self.assertNotIn('privateSecret', stderr + self.log.read_text())

    def test_interrupt_is_recorded_with_exit_130(self):
        with patch('plm_moodle_sync.cli.run_sync', side_effect=KeyboardInterrupt):
            result, _, stderr = self.command()
        self.assertEqual(result, 130)
        self.assertIn('Interrupted.', stderr)
        self.assertIn('status=interrupted exit_code=130', self.log.read_text())

    def test_dry_run_is_identified_and_log_override_is_relative_to_cwd(self):
        with in_directory(self.root), patch('plm_moodle_sync.cli.run_sync'):
            result, _, _ = self.command('upload', '--dry-run', '--log-file', 'outside-config/run.log')
        path = self.root / 'outside-config/run.log'
        self.assertEqual(result, 0)
        self.assertIn('mode=dry-run', path.read_text())
        self.assertFalse(self.log.exists())

    def test_yaml_log_path_is_relative_to_config_directory(self):
        self.data['sync'] = {'log_file': '../logs/jobs.log'}
        self.write_config()
        with in_directory(self.root), patch('plm_moodle_sync.cli.run_sync'):
            self.assertEqual(self.command()[0], 0)
        self.assertIn('status=success', (self.root / 'logs/jobs.log').read_text())

    def test_log_cannot_append_to_config_sessions_state_backups_or_project_cache(self):
        for target in ('sync.yaml', '.cache/sync.json', 'auth/plmlatex.json', '.cache/files/run.log'):
            with self.subTest(target=target), patch('plm_moodle_sync.cli.run_sync') as run:
                before = self.config_path.read_bytes()
                result, _, _ = self.command('upload', '--log-file', str(self.config_path.parent / target))
                self.assertEqual(result, 1)
                run.assert_not_called()
                self.assertEqual(self.config_path.read_bytes(), before)
        self.data['sync'] = {'log_file': '../logs/run.log', 'state_file': '../logs/run.log.1'}
        self.write_config()
        with self.assertRaisesRegex(ValueError, 'backups'):
            load_config(self.config_path)

    def test_unwritable_log_stops_before_pipeline_execution(self):
        self.log.mkdir(parents=True)
        with patch('plm_moodle_sync.cli.run_sync') as run:
            result, _, stderr = self.command()
        self.assertEqual(result, 1)
        run.assert_not_called()
        self.assertIn('Cannot write the persistent log', stderr)
        self.assertNotIn('Traceback', stderr)

    def test_mid_run_log_failure_stops_work_with_clear_error(self):
        from plm_moodle_sync.common.runlog import _Handler
        original_open = _Handler._open
        calls = []
        continued = Mock()

        def open_log(handler):
            calls.append(True)
            if len(calls) == 3:  # After start and configuration events.
                raise OSError('private exception details')
            return original_open(handler)

        def run(*_):
            report('About to upload TD1')
            continued()

        with patch.object(_Handler, '_open', open_log), patch('plm_moodle_sync.cli.run_sync', side_effect=run):
            result, _, stderr = self.command()
        self.assertEqual(result, 1)
        continued.assert_not_called()
        self.assertIn('Cannot write the persistent log', stderr)
        self.assertNotIn('private exception details', stderr + self.log.read_text())

    def test_rotation_is_bounded_and_retains_private_permissions(self):
        with patch('plm_moodle_sync.common.runlog.MAX_BYTES', 350), log_run(self.log, 'sync') as run:
            for number in range(20):
                log_event(f'File {number}: ' + 'x' * 80)
            run.finish(0)
        self.assertIn('status=success', self.log.read_text())
        backups = sorted(self.log.parent.glob('sync.log.[0-9]*'))
        self.assertEqual([path.suffix for path in backups], ['.1', '.2', '.3'])
        for path in [self.log, *backups]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_interleaved_writers_follow_rotation_and_remain_identifiable(self):
        with patch('plm_moodle_sync.common.runlog.MAX_BYTES', 350):
            with log_run(self.log, 'fetch') as first, log_run(self.log, 'upload') as second:
                first.write('First writer before rotation ' + 'x' * 140)
                second.write('Second writer forces rotation ' + 'y' * 140)
                first.write('First writer after rotation')
        self.assertIn('First writer after rotation', self.log.read_text())
        combined = ''.join(path.read_text() for path in self.log.parent.glob('sync.log*'))
        self.assertIn(first.run_id, combined)
        self.assertIn(second.run_id, combined)

    def test_only_application_events_are_logged_and_handlers_do_not_leak(self):
        with log_run(self.log, 'upload') as run, contextlib.redirect_stdout(io.StringIO()):
            report('A message\nwith a newline')
            print('third-party stdout secret')
            unrelated = logging.Logger('third-party')
            unrelated.addHandler(logging.NullHandler())
            unrelated.warning('third-party logging secret')
            run.finish(0)
        content = self.log.read_text()
        self.assertIn(r'A message\nwith a newline', content)
        self.assertNotIn('secret', content)
        log_event('Outside the finished run')
        self.assertEqual(self.log.read_text(), content)
        self.assertEqual(run.logger.handlers, [])

    def test_invalid_yaml_is_reported_before_opening_a_log(self):
        self.config_path.write_text('moodle: [privateSecret')
        with patch('plm_moodle_sync.cli.run_sync') as run:
            result, _, stderr = self.command()
        self.assertEqual(result, 1)
        run.assert_not_called()
        self.assertFalse(self.log.exists())
        self.assertNotIn('privateSecret', stderr)
