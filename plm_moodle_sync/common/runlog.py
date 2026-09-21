"""Persistent, rotating application logs without capturing third-party output."""

from contextlib import contextmanager
from contextvars import ContextVar
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import time
from uuid import uuid4


DEFAULT_LOG_FILE = Path('.cache/sync.log')
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3
_active = ContextVar('plm_moodle_sync_run_log', default=None)


class LogError(OSError):
    """Persistent logging is unavailable; no raw logging exception is exposed."""


def validate_log_file(path, *, protected=(), cache_dir=None):
    """Log append/rotation must never modify configuration, sessions or cache."""
    path = Path(path).expanduser().resolve()
    managed = [path, Path(str(path) + '.lock'),
               *(Path(str(path) + f'.{index}') for index in range(1, BACKUP_COUNT + 1))]
    for candidate in managed:
        resolved = candidate.resolve()
        for item in protected:
            item = Path(item).expanduser().resolve()
            if resolved == item or (candidate.exists() and item.exists() and candidate.samefile(item)):
                raise ValueError('The log file, backups and log lock must be separate from configuration, sessions and sync state.')
        if cache_dir is not None and resolved.is_relative_to(Path(cache_dir).expanduser().resolve()):
            raise ValueError('The log file and backups must be outside sync.cache_dir, which contains project files.')
    return path


def _private_open(path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        return os.fdopen(descriptor, 'a', encoding='utf-8')
    except BaseException:
        os.close(descriptor)
        raise


class _Handler(RotatingFileHandler):
    """Serialize append and rotation across processes sharing the same log."""
    def _open(self):
        return _private_open(self.baseFilename)

    def emit(self, record):
        import fcntl

        # Reopen after acquiring the lock: another process may have rotated it.
        with _private_open(self.baseFilename + '.lock') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                super().emit(record)
            finally:
                if self.stream is not None:
                    self.stream.close()
                    self.stream = None
                fcntl.flock(lock, fcntl.LOCK_UN)

    def handleError(self, record):
        # Standard logging's fallback prints the raw record/traceback to stderr.
        raise LogError('Cannot write the persistent log; check its directory and available disk space.')


class RunLog:
    def __init__(self, path, command, *, dry_run=False):
        self.path = Path(path)
        self.command = command
        self.started = time.monotonic()
        self.failed = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handler = _Handler(self.path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT,
                                encoding='utf-8', delay=True)
        formatter = logging.Formatter('%(asctime)s %(levelname)s run=%(run_id)s %(message)s',
                                      datefmt='%Y-%m-%dT%H:%M:%S%z')
        self.handler.setFormatter(formatter)
        self.logger = logging.Logger('plm_moodle_sync.run', level=logging.INFO)
        self.logger.propagate = False
        self.logger.addHandler(self.handler)
        self.run_id = uuid4().hex[:12]
        try:
            self.write('Run started: command=' + command + (' mode=dry-run' if dry_run else ' mode=normal'))
        except BaseException:
            self.close()
            raise

    def write(self, message, *, level=logging.INFO):
        if self.failed:
            return
        # Keep each event on one line, even for JSON reports or external names.
        message = str(message).replace('\r', '\\r').replace('\n', '\\n')
        try:
            self.logger.log(level, message, extra={'run_id': self.run_id})
        except OSError as error:
            self.failed = True
            raise LogError('Cannot write the persistent log; check its directory and available disk space.') from error

    def finish(self, exit_code):
        status = 'success' if exit_code == 0 else ('interrupted' if exit_code == 130 else 'failed')
        level = logging.INFO if exit_code == 0 else (logging.WARNING if exit_code == 130 else logging.ERROR)
        self.write(f'Run finished: command={self.command} status={status} exit_code={exit_code} '
                   f'duration_seconds={time.monotonic() - self.started:.3f}', level=level)

    def close(self):
        self.logger.removeHandler(self.handler)
        self.handler.close()


@contextmanager
def log_run(path, command, *, dry_run=False):
    run = RunLog(path, command, dry_run=dry_run)
    token = _active.set(run)
    try:
        yield run
    finally:
        _active.reset(token)
        run.close()


def log_event(message, *, level=logging.INFO):
    """Record an application event if a CLI run enabled persistent logging."""
    run = _active.get()
    if run is not None:
        run.write(message, level=level)


def report(message, *, error=False):
    """Write an application message to the console and the active run log."""
    print(message, file=sys.stderr if error else sys.stdout, flush=True)
    log_event(message, level=logging.ERROR if error else logging.INFO)
