"""Coordinate source retrieval, Moodle publication, and validation."""

import json
import re
from contextlib import nullcontext
from hashlib import sha256
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from urllib.parse import quote

from .config import ConfigError
from .plmlatex.auth import load_session
from .plmlatex.client import PLMlatexClient
from .plmlatex.errors import CompilationError
from .plmlatex.fetch import compile_documents
from .moodle import auth as moodle_auth
from .moodle.client import MoodleClient, resolve_section
from .moodle.settings import DEFAULT_MOODLE_SERVER, DEFAULT_MOODLE_COOKIE_FILE
from .moodle.errors import MoodleError
from .moodle.publish import FilePublisher
from .moodle.html import visible_text
from .state import load_state, save_state, state_lock
from .common.runlog import log_event, report
from .common.pdf import PDFError, extract_pages


def cached_pdf(config, mapping, state):
    """Verify the complete cached build, then prepare the target PDF and its hash."""
    source = mapping.source
    project = config.plmlatex.server + '/project/' + config.plmlatex.project_id
    build = state['projects'].get(project, {}).get('builds', {}).get(source.tex, {})
    relative = str(PurePosixPath(source.tex).with_suffix('.pdf'))
    if build.get('pdf') != relative:
        raise ConfigError(f'No verified cached build for {source.tex}; run fetch or sync first.')
    path = config.sync.cache_dir / config.plmlatex.project_id / Path(relative)
    content = path.read_bytes()
    digest = sha256(content).hexdigest()
    if not content.startswith(b'%PDF-') or digest != build.get('sha256'):
        raise ConfigError(f'Cached PDF for {source.tex} differs from its successful build; run fetch or sync first.')
    if mapping.target is not None and mapping.target.pages is not None:
        try:
            content = extract_pages(content, mapping.target.pages)
        except PDFError as error:
            raise ConfigError(f'{source.tex}: {error}') from error
        digest = sha256(content).hexdigest()
        log_event(f'Extracted pages {mapping.target.pages} from {source.tex} for {mapping.target.filename}.')
    return content, digest


def publication_module(course, section, filename, record, *, name=None):
    """Resolve a resource by its saved ID, allowing a configured display rename."""
    desired = visible_text(name if name is not None else filename)
    matches = [module for module in section['modules'] if module['name'] == desired]
    cmid = record.get('cmid')
    if cmid is not None and (type(cmid) is not int or cmid <= 0):
        raise ConfigError('Invalid Moodle resource ID in the publication state.')
    recorded = next((module for row in course['sections'] for module in row['modules'] if module['id'] == cmid), None)
    if len(matches) > 1 or (matches and matches[0]['modname'] != 'resource'):
        raise MoodleError(f'The Moodle destination {desired!r} is ambiguous or is not a File resource.')
    if recorded is not None:
        allowed_names = {desired, visible_text(record.get('name', filename))}
        if 'pending_name' in record:
            allowed_names.add(visible_text(record['pending_name']))
        if recorded not in section['modules'] or recorded['name'] not in allowed_names:
            raise MoodleError(f'Managed Moodle resource {cmid} was moved or renamed; review the configuration.')
        if recorded['modname'] != 'resource' or (matches and matches[0]['id'] != cmid):
            raise MoodleError(f'The Moodle destination {desired!r} conflicts with another resource.')
        return recorded
    if not matches and cmid is None and 'pending_sha256' in record:
        # A create may have completed before its ID could be recorded. Recover
        # it even if the configured display name changed before this retry.
        pending_name = visible_text(record.get('pending_name', filename))
        matches = [module for module in section['modules'] if module['name'] == pending_name]
        if len(matches) > 1 or (matches and matches[0]['modname'] != 'resource'):
            raise MoodleError('The pending Moodle publication is ambiguous; review it before syncing.')
    return matches[0] if matches else None


def upload_configured(config, *, timeout=180, dry_run=False, force=False):
    """Publish verified cached PDFs; caller holds the state lock for real writes."""
    mappings = [mapping for mapping in config.files if mapping.target]
    if not mappings:
        raise ConfigError('The configuration has no Moodle targets to upload.')
    state = load_state(config.sync.state_file)
    publications = state.setdefault('publications', {})
    if not isinstance(publications, dict):
        raise ConfigError('Invalid publication state; restore the sync state before uploading.')
    # Validate every cache before authentication or any Moodle write.
    cached = [(mapping, *cached_pdf(config, mapping, state)) for mapping in mappings]
    cookies = moodle_auth.load_cookies(config.moodle.cookie_file, config.moodle.server)
    reports = []
    with MoodleClient(config.moodle.server, cookies, timeout=timeout) as client:
        publisher = FilePublisher(client)
        course_id = config.moodle.course_id
        log_event(f'Checking Moodle course {course_id} on {client.server}.')
        course = client.course(course_id)
        planned, destinations, names = [], set(), set()
        for mapping, content, digest in cached:
            target = mapping.target
            section = resolve_section(course['sections'], target.section)
            key = (client.server + f'/course/{course_id}/section/{section["id"]}/file/'
                   + quote(target.filename, safe=''))
            if key in destinations:
                raise ConfigError('Several mappings resolve to the same Moodle destination.')
            destinations.add(key)
            display = (section['id'], visible_text(target.name))
            if display in names:
                raise ConfigError('Several mappings use the same Moodle display name in one section.')
            names.add(display)
            record = publications.get(key, {})
            if not isinstance(record, dict):
                raise ConfigError('Invalid publication record; restore the sync state before uploading.')
            module = publication_module(course, section, target.filename, record, name=target.name)
            remote = publisher.pdf_hash(module['id'], target.filename) if module else None
            # Recover an interrupted save, or safely adopt an identical PDF.
            if remote is not None and remote not in {digest, record.get('sha256'), record.get('pending_sha256')}:
                raise MoodleError(f'{target.filename!r} already exists with unrecognized content; review it in Moodle before syncing.')
            visibility_matches = (target.visible is None or (module is not None and
                                  client.resource_visibility(course_id, module['id']) == int(target.visible)))
            unchanged = (remote == digest and module['name'] == visible_text(target.name)
                         and visibility_matches and not force)
            action = 'unchanged' if unchanged else ('update' if module else 'create')
            planned.append((target, content, digest, section, key, record, module, remote, action))
        # All destinations and collisions have been checked before the first upload.
        for target, content, digest, section, key, record, module, remote, action in planned:
            cmid = module['id'] if module else None
            if not dry_run and action != 'unchanged':
                # Recheck immediately before editing; avoid acting on stale preflight data.
                current = client.course(course_id)
                current_section = resolve_section(current['sections'], target.section)
                if current_section['id'] != section['id']:
                    raise MoodleError('The Moodle section changed during synchronization; retry.')
                current_module = publication_module(current, current_section, target.filename, record, name=target.name)
                if (current_module['id'] if current_module else None) != cmid:
                    raise MoodleError('The Moodle destination changed during synchronization; retry.')
                if cmid and current_module['name'] != module['name']:
                    raise MoodleError('The Moodle display name changed during synchronization; retry.')
                if cmid and publisher.pdf_hash(cmid, target.filename) != remote:
                    raise MoodleError('The Moodle PDF changed during synchronization; retry.')

                def pending():
                    publications[key] = {**record, 'pending_sha256': digest}
                    if cmid:
                        publications[key]['cmid'] = cmid
                    if target.name != target.filename or 'name' in record or 'pending_name' in record:
                        publications[key]['pending_name'] = target.name
                    save_state(config.sync.state_file, state)

                replace_pdf = force or remote != digest
                operation = 'Uploading' if replace_pdf else 'Updating settings for'
                report(f'{operation} {target.filename} → course {course_id}, {current_section["name"]}'
                       + (f' (resource {cmid})' if cmid else ' (new resource)'))
                publisher.publish(course_id, current_section['number'], target.filename, content,
                                  cmid=cmid, name=target.name, visible=target.visible,
                                  replace_pdf=replace_pdf, before_submit=pending)
                updated = client.course(course_id)
                updated_section = resolve_section(updated['sections'], target.section)
                module = publication_module(updated, updated_section, target.filename, {'cmid': cmid} if cmid else {},
                                            name=target.name)
                if (module is None or (cmid is not None and module['id'] != cmid)
                        or module['name'] != visible_text(target.name)
                        or publisher.pdf_hash(module['id'], target.filename) != digest):
                    raise MoodleError('Published PDF verification failed; success was not recorded. Rerun upload to reconcile.')
                cmid = module['id']
                if target.visible is not None and client.resource_visibility(course_id, cmid) != int(target.visible):
                    raise MoodleError('Published visibility verification failed; success was not recorded. Rerun upload to reconcile.')
            if not dry_run:
                publications[key] = {'cmid': cmid, 'sha256': digest}
                if target.name != target.filename:
                    publications[key]['name'] = target.name
                save_state(config.sync.state_file, state)
            result = {'filename': target.filename, 'name': target.name, 'action': action, 'course_id': course_id,
                      'section_name': section['name'], 'section_id': section['id'], 'cmid': cmid}
            if target.visible is not None:
                result['visible'] = target.visible
            if target.pages is not None:
                result['pages'] = target.pages
            reports.append(result)
            prefix = 'Would ' if dry_run else ''
            label = 'leave unchanged' if dry_run and action == 'unchanged' else action
            report(f'{prefix}{label}: {target.filename} → course {course_id}, {section["name"]}'
                  + (f' as {target.name!r}' if target.name != target.filename else '')
                  + (f' (resource {cmid})' if cmid else '')
                  + (f'; pages: {target.pages}' if target.pages is not None else '')
                  + (f'; visibility: {"visible" if target.visible else "hidden"}' if target.visible is not None else ''))
    return reports


def run_sync(args, config):
    if not any(mapping.target for mapping in config.files):
        raise ConfigError('The configuration has no Moodle targets to upload.')
    with nullcontext() if args.dry_run else state_lock(config.sync.state_file):
        if args.command == 'sync':
            fetch_args = SimpleNamespace(server=config.plmlatex.server, cookie_file=config.plmlatex.cookie_file,
                                         output_dir=config.sync.cache_dir, state_file=config.sync.state_file,
                                         timeout=args.timeout, force=False, list_documents=False,
                                         project_id=None, tex=[])
            fetch_configured(fetch_args, config)
        return upload_configured(config, timeout=args.timeout, dry_run=args.dry_run, force=args.force)


def run_moodle(args, config):
    server = args.server or (config.moodle.server if config else DEFAULT_MOODLE_SERVER)
    cookie_file = args.cookie_file or (config.moodle.cookie_file if config else DEFAULT_MOODLE_COOKIE_FILE)
    if config and cookie_file.expanduser().resolve() in {config.plmlatex.cookie_file, config.sync.state_file}:
        raise ConfigError('The Moodle session must not overwrite the PLMlatex session or sync state.')
    if args.command == 'moodle-login':
        print('Complete Moodle institutional sign-in in the browser window.', flush=True)
        moodle_auth.login(cookie_file, server=server, timeout=args.timeout)
        print('Moodle session saved to ' + str(cookie_file) + '.', flush=True)
        return
    course_id = args.course_id if args.course_id is not None else (config.moodle.course_id if config else None)
    if course_id is None or course_id <= 0:
        raise ConfigError('Set moodle.course_id or --course-id to a positive integer.')
    cookies = moodle_auth.load_cookies(cookie_file, server)
    with MoodleClient(server, cookies, timeout=args.timeout) as client:
        result = client.check(course_id, section_name=args.section_name)
        if config and not args.section_name:
            for file in config.files:
                if file.target:
                    resolve_section(result['sections'], file.target.section)
        print(json.dumps(result, indent=2, ensure_ascii=False), flush=True)


def configured_sources(args, config):
    """Select TeX paths in the single project, fetching shared sources once."""
    if config.plmlatex.project_id is None:
        raise ConfigError('Set plmlatex.project_id for this configuration.')
    paths = list(dict.fromkeys(file.source.tex for file in config.files))
    if args.tex:
        selected = {PLMlatexClient.validate_tex_path(path) for path in args.tex}
        missing = selected - set(paths)
        if missing:
            raise ConfigError('--tex does not match a configured source: ' + ', '.join(sorted(missing)))
        paths = [path for path in paths if path in selected]
    if not paths and not args.list_documents:
        raise ConfigError('The configuration has no sources to fetch; add entries to files.')
    return paths


def fetch_configured(args, config):
    selected = SimpleNamespace(**vars(args))
    selected.project_id = config.plmlatex.project_id
    selected.project_name = None
    selected.tex = configured_sources(args, config)
    return fetch_documents(selected)


def fetch_documents(args):
    cookies = load_session(args.cookie_file, server=args.server)
    client = PLMlatexClient(cookie=cookies, base_url=args.server)
    project_id = args.project_id
    if args.project_name:
        project = client.get_project(args.project_name)
        if project is None:
            raise CompilationError('Project not found: ' + args.project_name)
        project_id = project.get('id') or project.get('_id')
        if not isinstance(project_id, str) or not re.fullmatch(r'[0-9a-fA-F]{24}', project_id):
            raise CompilationError('The project list returned an invalid project ID.')
        report('Resolved project {!r}: {}'.format(args.project_name, project_id))
    log_event('PLMlatex project: ' + args.server + '/project/' + project_id)
    if args.list_documents:
        client.refresh_csrf(project_id)
        project = client.get_project_infos(project_id)
        for path in sorted(client.document_paths(project)):
            report(path)
    else:
        compile_documents(client, project_id, args.tex, args.output_dir.expanduser(), args.timeout,
                          state_file=args.state_file.expanduser(), force=args.force)
