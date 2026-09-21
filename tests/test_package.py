"""Package boundaries and compatibility of persisted synchronization state."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from plm_moodle_sync.state import StateError, load_state, save_state


class PackageTests(unittest.TestCase):
    def test_service_modules_import_without_the_other_service_or_cli(self):
        for service, modules, blocked in [
            ('plmlatex', ['client', 'auth', 'dependencies', 'fetch'], ['plm_moodle_sync.moodle', 'py_moodle']),
            ('moodle', ['client', 'auth', 'publish'], ['plm_moodle_sync.plmlatex', 'socketIO_client']),
        ]:
            with self.subTest(service=service):
                code = '''
import importlib
import importlib.abc
import sys
blocked = BLOCKED + ['plm_moodle_sync.cli', 'plm_moodle_sync.config', 'plm_moodle_sync.sync', 'PySide6']
class BlockImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, BlockImports())
for module in MODULES:
    importlib.import_module('plm_moodle_sync.' + SERVICE + '.' + module)
'''.replace('BLOCKED', repr(blocked)).replace('MODULES', repr(modules)).replace('SERVICE', repr(service))
                result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_module_entry_point_exposes_all_commands(self):
        result = subprocess.run([sys.executable, '-m', 'plm_moodle_sync', '--help'],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in ('fetch', 'plmlatex-login', 'moodle-login', 'moodle-check', 'sync', 'upload'):
            self.assertIn(command, result.stdout)

    def test_existing_state_round_trips_and_preserves_unrelated_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sync.json'
            state = {'version': 2, 'projects': {'https://latex.example/project/id': {'builds': {}, 'sources': {}}},
                     'other_metadata': {'retained': True}}
            path.write_text(json.dumps(state))
            loaded = load_state(path)
            save_state(path, loaded)
            self.assertEqual(load_state(path), state)

    def test_missing_state_is_initialized_without_a_write_and_invalid_state_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sync.json'
            self.assertEqual(load_state(path), {'version': 2, 'projects': {}})
            self.assertFalse(path.exists())
            for content in ('invalid-json', '[]', '{"version":1,"projects":{}}'):
                path.write_text(content)
                with self.subTest(content=content), self.assertRaises(StateError):
                    load_state(path)
                self.assertEqual(path.read_text(), content)


if __name__ == '__main__':
    unittest.main()
