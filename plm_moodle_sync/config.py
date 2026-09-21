"""Load and validate YAML settings before opening a network connection."""

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re

import yaml

from .plmlatex.client import PLMlatexClient
from .plmlatex.settings import DEFAULT_COOKIE_FILE, DEFAULT_SERVER
from .moodle.settings import DEFAULT_MOODLE_COOKIE_FILE, DEFAULT_MOODLE_SERVER
from .moodle.html import visible_text
from .common.urls import normalize_server
from .common.pdf import page_bounds
from .common.runlog import DEFAULT_LOG_FILE, validate_log_file
from .state import DEFAULT_STATE_FILE


class ConfigError(ValueError):
    """The configuration cannot describe an unambiguous synchronization job."""


@dataclass(frozen=True)
class PLMlatexSettings:
    server: str
    cookie_file: Path
    project_id: str | None


@dataclass(frozen=True)
class MoodleSettings:
    server: str
    cookie_file: Path
    course_id: int | None


@dataclass(frozen=True)
class Source:
    tex: str


@dataclass(frozen=True)
class Target:
    section: str | int
    filename: str
    name: str
    visible: bool | None = None
    pages: str | None = None


@dataclass(frozen=True)
class FileMapping:
    source: Source
    target: Target | None


@dataclass(frozen=True)
class SyncSettings:
    state_file: Path
    cache_dir: Path
    log_file: Path


@dataclass(frozen=True)
class Config:
    path: Path
    plmlatex: PLMlatexSettings
    moodle: MoodleSettings
    files: tuple[FileMapping, ...]
    sync: SyncSettings


class ConfigLoader(yaml.SafeLoader):
    """Use plain YAML data and reject silently overwritten mapping keys."""

    def construct_mapping(self, node, deep=False):
        if not isinstance(node, yaml.MappingNode):
            return super().construct_mapping(node, deep=deep)
        self.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise ConfigError(f'YAML keys must be strings (line {key_node.start_mark.line + 1}).')
            if key in result:
                raise ConfigError(f'Duplicate YAML key (line {key_node.start_mark.line + 1}).')
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def mapping(value, location, allowed):
    if not isinstance(value, dict):
        raise ConfigError(location + ' must be a mapping.')
    unknown = set(value) - set(allowed)
    if unknown:
        raise ConfigError(location + ': unknown option(s): ' + ', '.join(sorted(unknown)))
    return value


def text(value, location):
    if not isinstance(value, str) or not value.strip() or any(char in value for char in '\x00\r\n'):
        raise ConfigError(location + ' must be a nonempty string on one line.')
    return value


def project_id(value, location):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F]{24}', value):
        raise ConfigError(location + ' must be a 24-character hexadecimal string (quote it in YAML).')
    return value.lower()


def course_id(value, location):
    if type(value) is not int or value <= 0:
        raise ConfigError(location + ' must be a positive integer.')
    return value


def section_selector(value, location):
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str):
        return text(value, location)
    raise ConfigError(location + ' must be a section name string or a positive integer section ID.')


def local_path(value, location, directory):
    path = Path(text(value, location)).expanduser()
    return (path if path.is_absolute() else directory / path).resolve()


def server_url(value, location):
    try:
        return normalize_server(text(value, location))
    except ValueError as error:
        raise ConfigError(location + ' must be an HTTPS URL without credentials, query, or fragment.') from error


def load_config(path):
    path = Path(path).expanduser().absolute()
    try:
        with path.open(encoding='utf-8') as stream:
            raw = yaml.load(stream, Loader=ConfigLoader)
    except (yaml.YAMLError, UnicodeError) as error:
        # YAML exceptions include snippets; avoid echoing secrets from bad input.
        mark = getattr(error, 'problem_mark', None)
        location = f' at line {mark.line + 1}, column {mark.column + 1}' if mark else ''
        raise ConfigError('Invalid YAML' + location + '.') from error
    root = mapping(raw, 'config', {'plmlatex', 'moodle', 'files', 'sync'})
    plm = mapping(root.get('plmlatex', {}), 'plmlatex', {'server', 'cookie_file', 'project_id'})
    moodle = mapping(root.get('moodle', {}), 'moodle', {'server', 'cookie_file', 'course_id'})
    sync = mapping(root.get('sync', {}), 'sync', {'state_file', 'cache_dir', 'log_file'})
    plm_settings = PLMlatexSettings(
        server_url(plm.get('server', DEFAULT_SERVER), 'plmlatex.server'),
        local_path(plm.get('cookie_file', str(DEFAULT_COOKIE_FILE)), 'plmlatex.cookie_file', path.parent),
        project_id(plm['project_id'], 'plmlatex.project_id') if 'project_id' in plm else None,
    )
    moodle_settings = MoodleSettings(
        server_url(moodle.get('server', DEFAULT_MOODLE_SERVER), 'moodle.server'),
        local_path(moodle.get('cookie_file', str(DEFAULT_MOODLE_COOKIE_FILE)), 'moodle.cookie_file', path.parent),
        course_id(moodle['course_id'], 'moodle.course_id') if 'course_id' in moodle else None,
    )
    sync_settings = SyncSettings(
        local_path(sync.get('state_file', str(DEFAULT_STATE_FILE)), 'sync.state_file', path.parent),
        local_path(sync.get('cache_dir', '.cache/files'), 'sync.cache_dir', path.parent),
        local_path(sync.get('log_file', str(DEFAULT_LOG_FILE)), 'sync.log_file', path.parent),
    )
    if sync_settings.state_file == plm_settings.cookie_file:
        raise ConfigError('sync.state_file and plmlatex.cookie_file must be different files.')
    if moodle_settings.cookie_file in {plm_settings.cookie_file, sync_settings.state_file}:
        raise ConfigError('moodle.cookie_file, plmlatex.cookie_file, and sync.state_file must be different files.')
    try:
        validate_log_file(sync_settings.log_file, protected=(
            path, plm_settings.cookie_file, moodle_settings.cookie_file,
            sync_settings.state_file, Path(str(sync_settings.state_file) + '.lock'),
        ), cache_dir=sync_settings.cache_dir)
    except ValueError as error:
        raise ConfigError(str(error)) from error

    rows = root.get('files', [])
    if not isinstance(rows, list):
        raise ConfigError('files must be a list.')
    if rows and plm_settings.project_id is None:
        raise ConfigError('Set plmlatex.project_id for the files in this configuration.')
    files, targets, names, outputs = [], set(), set(), {}
    for index, row in enumerate(rows):
        location = f'files[{index + 1}]'
        row = mapping(row, location, {'source', 'target'})
        source = mapping(row.get('source'), location + '.source', {'tex'})
        tex = text(source.get('tex'), location + '.source.tex')
        try:
            tex = PLMlatexClient.validate_tex_path(tex)
        except ValueError as error:
            raise ConfigError(location + '.source.tex: ' + str(error)) from error
        output = str(PurePosixPath(tex).with_suffix('.pdf'))
        if output in outputs and outputs[output] != tex:
            raise ConfigError(location + '.source.tex would overwrite another document PDF.')
        outputs[output] = tex
        target = None
        if 'target' in row:
            destination = mapping(row['target'], location + '.target', {'section', 'filename', 'name', 'visible', 'pages'})
            if moodle_settings.course_id is None:
                raise ConfigError('Set moodle.course_id for the targets in this configuration.')
            section = section_selector(destination.get('section'), location + '.target.section')
            name = text(destination.get('name'), location + '.target.name')
            default_filename = PurePosixPath(tex).with_suffix('.pdf').name
            filename = text(destination.get('filename', default_filename), location + '.target.filename')
            if '/' in filename or '\\' in filename:
                raise ConfigError(location + '.target.filename must be a PDF filename without a directory.')
            if PurePosixPath(filename).suffix.lower() != '.pdf':
                raise ConfigError(location + '.target.filename must end in .pdf; it names the downloadable PDF.')
            visible = destination.get('visible')
            if 'visible' in destination and type(visible) is not bool:
                raise ConfigError(location + '.target.visible must be true or false, or omitted to preserve visibility.')
            pages = None
            if 'pages' in destination:
                try:
                    first, last = page_bounds(destination['pages'])
                except ValueError as error:
                    raise ConfigError(location + '.target.pages: ' + str(error)) from error
                pages = str(first) if first == last else f'{first}-{last}'
            target = Target(section, filename, name, visible, pages)
            identity = (section, filename)
            if identity in targets:
                raise ConfigError(location + '.target duplicates another Moodle destination.')
            targets.add(identity)
            display = (section, visible_text(name))
            if not display[1]:
                raise ConfigError(location + '.target.name must contain visible text.')
            if display in names:
                raise ConfigError(location + '.target.name duplicates another Moodle display name in this section.')
            names.add(display)
        files.append(FileMapping(Source(tex), target))
    return Config(path, plm_settings, moodle_settings, tuple(files), sync_settings)
