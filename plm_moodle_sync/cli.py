"""Commands for PLMlatex retrieval and Moodle integration."""

import argparse
from pathlib import Path
import re

import requests

from . import __version__
from .config import ConfigError, load_config
from .common.runlog import DEFAULT_LOG_FILE, log_event, log_run, report, validate_log_file
from .moodle.errors import MoodleError
from .moodle.settings import DEFAULT_MOODLE_COOKIE_FILE
from .plmlatex.auth import LOGIN_HINT, login
from .plmlatex.errors import AuthenticationError, CompilationError
from .plmlatex.settings import DEFAULT_COOKIE_FILE, DEFAULT_SERVER
from .state import DEFAULT_STATE_FILE, state_lock
from .sync import configured_sources, fetch_configured, fetch_documents, run_moodle, run_sync


def parser_for_commands():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version=f'plm-moodle-sync {__version__}')
    commands = parser.add_subparsers(dest='command', required=True)
    fetch = commands.add_parser('fetch', help='Fetch changed PDFs or list project documents.')
    project = fetch.add_mutually_exclusive_group()
    project.add_argument('--project-id', help='24-character ID from the PLMlatex project URL.')
    project.add_argument('--project-name', help='Exact, unique project name on PLMlatex.')
    fetch.add_argument('--tex', action='append', default=[], help='TeX path to fetch; with --config, select matching configured files.')
    fetch.add_argument('--list-documents', action='store_true', help='List TeX paths without compiling.')
    fetch.add_argument('--output-dir', type=Path, help='Override sync.cache_dir (default: .cache/files).')
    fetch.add_argument('--state-file', type=Path, help='Override sync.state_file (default: .cache/sync.json).')
    fetch.add_argument('--force', action='store_true', help='Rebuild PDFs even when their inputs are unchanged.')
    fetch.add_argument('--timeout', type=int, default=180, help='Compile/download HTTP timeout in seconds.')
    auth = commands.add_parser('plmlatex-login', help='Create or renew a PLMlatex session through browser sign-in.')
    auth.add_argument('--timeout', type=int, default=30, help='HTTP timeout when validating the new session.')
    for command in (fetch, auth):
        command.add_argument('--config', type=Path, nargs='+', action='extend', help='One or more YAML files, processed in order; paths inside each are relative to its directory.')
        command.add_argument('--cookie-file', type=Path, help='Override the session path (default: .secrets/plmlatex.json).')
        command.add_argument('--server', help='Override the PLMlatex server URL.')
    moodle_auth = commands.add_parser('moodle-login', help='Save a Moodle session through institutional browser sign-in.')
    moodle_check = commands.add_parser('moodle-check', help='Check Moodle course sections and File forms without publishing.')
    for command in (moodle_auth, moodle_check):
        command.add_argument('--config', type=Path, nargs='+', action='extend', help='One or more YAML configuration files, processed in order.')
        command.add_argument('--server', help='Override the Moodle server URL.')
        command.add_argument('--cookie-file', type=Path, help='Override moodle.cookie_file (default: .secrets/moodle.json).')
        command.add_argument('--timeout', type=int, default=30, help='HTTP timeout in seconds.')
    moodle_check.add_argument('--course-id', type=int, help='Override moodle.course_id for the check.')
    moodle_check.add_argument('--section-name', help='Check one exact, unique section name; otherwise list all sections.')
    sync = commands.add_parser('sync', help='Fetch changed PDFs, then create/update configured Moodle File resources.')
    upload = commands.add_parser('upload', help='Create/update Moodle File resources from verified cached PDFs.')
    for command in (sync, upload):
        command.add_argument('--config', type=Path, nargs='+', action='extend', required=True, help='One or more YAML configuration files, processed in order.')
        command.add_argument('--timeout', type=int, default=180, help='HTTP timeout in seconds.')
        command.add_argument('--force', action='store_true', help='Replace Moodle PDFs even when unchanged; does not force compilation.')
    upload.add_argument('--dry-run', action='store_true', help='Preview uploads; write only the run log, leaving Moodle and sync state unchanged.')
    sync.set_defaults(dry_run=False)
    for command in (fetch, sync, upload):
        command.add_argument('--log-file', type=Path, help='Override sync.log_file (default: .cache/sync.log).')
    return parser


def apply_settings(args, config):
    """Explicit CLI values take precedence; YAML paths are already resolved."""
    if args.server is None:
        args.server = config.plmlatex.server if config else DEFAULT_SERVER
    if args.cookie_file is None:
        args.cookie_file = config.plmlatex.cookie_file if config else DEFAULT_COOKIE_FILE
    if args.command == 'fetch':
        if args.output_dir is None:
            args.output_dir = config.sync.cache_dir if config else Path('.cache/files')
        if args.state_file is None:
            args.state_file = config.sync.state_file if config else DEFAULT_STATE_FILE
        if args.state_file.expanduser().resolve() == args.cookie_file.expanduser().resolve():
            raise ConfigError('The state file and cookie file must be different files.')


def main(argv=None):
    parser = parser_for_commands()
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error('--timeout must be positive.')
    if args.command == 'fetch':
        if args.project_id and not re.fullmatch(r'[0-9a-fA-F]{24}', args.project_id):
            parser.error('--project-id must be the 24-character hexadecimal ID from the project URL.')
        if args.config and (args.project_name or args.project_id):
            parser.error('--project-id and --project-name are for use without --config; set plmlatex.project_id in each YAML file.')
        if not args.config and not (args.project_id or args.project_name):
            parser.error('Specify --config, --project-id, or --project-name.')
        if (args.tex and args.list_documents) or (not args.config and not args.tex and not args.list_documents):
            parser.error('Choose either --list-documents or one or more --tex arguments.')
    try:
        # Load the entire batch before any job can authenticate or write files.
        paths = list(dict.fromkeys(path.expanduser().resolve() for path in (args.config or [])))
        configs = []
        for path in paths:
            try:
                configs.append(load_config(path))
            except (OSError, ValueError) as error:
                raise ConfigError(f'{path}: {error}') from error
        if not configs:
            configs.append(None)
        jobs = []
        for config in configs:
            job = argparse.Namespace(**vars(args))
            job.config = config.path if config else None
            if job.command in ('fetch', 'plmlatex-login'):
                apply_settings(job, config)
                if job.command == 'fetch' and config:
                    configured_sources(job, config)
            if job.command in ('sync', 'upload') and not any(file.target for file in config.files):
                raise ConfigError('The configuration has no Moodle targets to upload: ' + str(config.path))
            jobs.append((job, config))
        log_paths = batch_log_paths(jobs)
    except (OSError, ValueError) as error:
        # Invalid configuration is reported before selecting/opening any log.
        report('Error: ' + str(error), error=True)
        return 1
    result = 0
    for (job, config), log_path in zip(jobs, log_paths):
        if len(jobs) > 1:
            report('Configuration: ' + str(config.path))
        status = execute_logged(job, config, log_path)
        if status == 130:
            return status
        if status:
            result = 1
    return result


def batch_log_paths(jobs):
    """Protect every configuration's sessions, state, and cache from every log."""
    if jobs[0][0].command not in ('fetch', 'sync', 'upload'):
        return [None] * len(jobs)
    protected, caches, paths = [], [], []
    for args, config in jobs:
        path = args.log_file or (config.sync.log_file if config else DEFAULT_LOG_FILE)
        state = getattr(args, 'state_file', None) or (config.sync.state_file if config else DEFAULT_STATE_FILE)
        cache = getattr(args, 'output_dir', None) or (config.sync.cache_dir if config else Path('.cache/files'))
        protected += [state, Path(str(state) + '.lock'),
                     getattr(args, 'cookie_file', None) or (config.plmlatex.cookie_file if config else DEFAULT_COOKIE_FILE),
                     config.moodle.cookie_file if config else DEFAULT_MOODLE_COOKIE_FILE]
        if config:
            protected += [config.path, config.sync.state_file, Path(str(config.sync.state_file) + '.lock'),
                          config.plmlatex.cookie_file]
            caches.append(config.sync.cache_dir)
        paths.append(path)
        caches.append(cache)
    validated = []
    for path in paths:
        for cache in caches:
            path = validate_log_file(path, protected=protected, cache_dir=cache)
        validated.append(path)
    return validated


def execute_logged(args, config, log_path):
    if log_path is not None:
        try:
            with log_run(log_path, args.command, dry_run=getattr(args, 'dry_run', False)) as run:
                if config:
                    log_event('Configuration: ' + str(config.path))
                result = _execute(args, config)
                run.finish(result)
                return result
        except ValueError as error:
            report('Error: ' + str(error), error=True)
            return 1
        except OSError:
            report('Error: Cannot write the persistent log; check its directory and available disk space.', error=True)
            return 1
    return _execute(args, config)


def _execute(args, config):
    try:
        if args.command in ('sync', 'upload'):
            run_sync(args, config)
            return 0
        if args.command.startswith('moodle-'):
            run_moodle(args, config)
            return 0
        if args.command == 'plmlatex-login':
            report('Complete institutional sign-in in the browser window.')
            login(args.cookie_file, server=args.server, timeout=args.timeout)
            report('Session saved to ' + str(args.cookie_file.expanduser()) + '.')
        elif config is not None:
            # Validate every mapping and selection before touching authentication,
            # project caches, or the network.
            with state_lock(args.state_file):
                fetch_configured(args, config)
        else:
            with state_lock(args.state_file):
                fetch_documents(args)
    except (CompilationError, MoodleError, OSError, ValueError) as error:
        # requests exceptions may contain signed URLs. Avoid printing them.
        if isinstance(error, requests.RequestException):
            service = 'Synchronization' if args.command == 'sync' else ('Moodle' if args.command.startswith('moodle-') or args.command == 'upload' else 'PLMlatex')
            message = service + ' network request failed; check connectivity and retry.'
        else:
            message = str(error)
        if isinstance(error, AuthenticationError) and args.command == 'fetch' and LOGIN_HINT not in message:
            message += ' ' + LOGIN_HINT
        if isinstance(error, AuthenticationError) and args.command == 'fetch' and args.config is not None:
            message += ' Reuse --config when running plmlatex-login, along with any --server or --cookie-file overrides.'
        report('Error: ' + message, error=True)
        return 1
    except KeyboardInterrupt:
        report('Interrupted.', error=True)
        return 130
    except Exception as error:
        # Unexpected exceptions can include credentials/HTML in their messages.
        report('Error: Unexpected runtime failure (' + type(error).__name__ + ').', error=True)
        return 1
    return 0
