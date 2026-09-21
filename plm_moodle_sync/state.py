"""Read, lock, and atomically save synchronization state."""

import json
from contextlib import contextmanager
from pathlib import Path

from .common.files import atomic_write, encoded_json


DEFAULT_STATE_FILE = Path('.cache/sync.json')
SCHEMA = 2


class StateError(ValueError):
    """Synchronization state cannot be safely read or locked."""


def load_state(path):
    path = Path(path)
    if not path.exists():
        return {'version': SCHEMA, 'projects': {}}
    try:
        state = json.loads(path.read_text())
    except (ValueError, UnicodeError) as error:
        raise StateError('Cannot read the sync state; restore it or use a new state file.') from error
    if not isinstance(state, dict) or state.get('version') != SCHEMA or not isinstance(state.get('projects'), dict):
        raise StateError('Unrecognized sync state format; use a new state file.')
    return state


def save_state(path, state, *, write=atomic_write):
    write(Path(path), encoded_json(state))


@contextmanager
def state_lock(path):
    """Prevent overlapping commands sharing a state file (Linux/macOS)."""
    import fcntl

    lock = Path(str(path) + '.lock')
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise StateError('Another sync command is using this state file; retry when it finishes.') from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
