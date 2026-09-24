"""Cache source revisions, discovered dependencies, and successfully built PDFs."""

import hashlib
from pathlib import PurePosixPath

from .errors import CompilationError
from ..common.files import atomic_write, encoded_json as encoded
from ..common.runlog import report
from .dependencies import TEXT_SUFFIXES, discover, project_path, recorder_inputs
from ..state import DEFAULT_STATE_FILE, StateError, load_state, save_state


def digest(content):
    return hashlib.sha256(content).hexdigest()


def changed_paths(label, paths):
    """Keep rebuild explanations useful even for a large shared dependency set."""
    paths = sorted(paths)
    return label + ': ' + ', '.join(paths[:5]) + (f' (+{len(paths) - 5} more)' if len(paths) > 5 else '')


def source_locations(entries):
    """Mirror project paths while reserving compiled PDF names for every TeX file."""
    pdfs = {str(PurePosixPath(path).with_suffix('.pdf'))
            for path in entries if PurePosixPath(path).suffix.lower() == '.tex'}
    folders = {str(parent) for path in entries for parent in PurePosixPath(path).parents if str(parent) != '.'}
    if pdfs & folders:
        raise CompilationError('A project directory conflicts with a compiled PDF filename: ' + sorted(pdfs & folders)[0])
    occupied = set(entries) | pdfs | folders
    locations = {}
    for path in sorted(entries):
        candidate = path
        if path in pdfs:
            original = PurePosixPath(path)
            index = 1
            while candidate in occupied:
                suffix = '.source' if index == 1 else f'.source-{index}'
                candidate = str(original.with_name(original.stem + suffix + original.suffix))
                index += 1
            occupied.add(candidate)
        locations[path] = candidate
    return locations


class Sources:
    """Read each shared input at most once during the initial change check."""

    def __init__(self, client, socket, project_id, entries, directory, previous, write):
        self.client, self.socket, self.project_id = client, socket, project_id
        self.entries, self.directory, self.previous = entries, directory, previous
        self.write = write
        self.records, self.contents = {}, {}
        self.locations = source_locations(entries)

    def read_document(self, path, previous_version):
        try:
            return self.client.read_document(self.socket, self.entries[path]['id'], previous_version)
        except CompilationError as error:
            raise CompilationError(f'PLMlatex source {path}: {error}') from error

    def get(self, path):
        if path in self.records:
            return self.records[path]
        if path not in self.entries:
            raise CompilationError('A required project file is missing: ' + path)
        entry = self.entries[path]
        old = self.previous.get(path, {})
        local = self.directory / self.locations[path]
        cached = local.read_bytes() if local.is_file() else None
        reuse = (old.get('id') == entry['id'] and old.get('kind') == entry['kind']
                 and cached is not None and digest(cached) == old.get('sha256'))
        record = {'id': entry['id'], 'kind': entry['kind']}
        if self.locations[path] != path:
            record['cache_path'] = self.locations[path]
        if entry['kind'] == 'doc':
            previous_version = old.get('version', -1) if reuse else -1
            snapshot = self.read_document(path, previous_version)
            if snapshot['version'] == previous_version and reuse:
                content = cached
            elif snapshot['text'] is not None:
                content = snapshot['text'].encode('utf8')
            else:
                raise CompilationError('Source content missing after a revision change: ' + path)
            record['version'] = snapshot['version']
        else:
            metadata = {key: entry[key] for key in ('hash', 'rev') if key in entry}
            if metadata and reuse and old.get('metadata') == metadata:
                content = cached
            else:
                content = self.client.download_file(self.project_id, entry['id'])
            record['metadata'] = metadata
        record['sha256'] = digest(content)
        self.records[path], self.contents[path] = record, content
        if content != cached:
            self.write(local, content)
        return record

    def graph(self, root, initial=()):
        dependencies, uncertain = set(), []
        pending = [root, *initial]
        # latexmk configuration can affect the build without being a TeX input.
        pending.extend(path for path in ('latexmkrc', '.latexmkrc') if path in self.entries)
        while pending:
            path = pending.pop()
            if path in dependencies:
                continue
            self.get(path)
            dependencies.add(path)
            if PurePosixPath(path).suffix.lower() in TEXT_SUFFIXES:
                text = self.contents[path].decode('utf8', errors='replace')
                children, problems = discover(text, path, root, self.entries)
                pending.extend(children - dependencies)
                uncertain.extend(path + ': ' + problem for problem in problems)
        if uncertain or any(path in self.entries for path in ('latexmkrc', '.latexmkrc')):
            # Dynamic TeX paths or executable build configuration require a
            # conservative fallback. No manual dependency configuration needed.
            dependencies.update(self.entries)
            for path in sorted(dependencies):
                self.get(path)
        return dependencies, sorted(set(uncertain))

    def verify(self, dependencies):
        """Reject a build if a watched input was edited while it compiled."""
        for path in sorted(dependencies):
            before = self.records[path]
            if before['kind'] == 'doc':
                after = self.read_document(path, before['version'])
                if after['version'] != before['version']:
                    raise CompilationError('Source changed during compilation; retry: ' + path)
            else:
                content = self.client.download_file(self.project_id, before['id'])
                if digest(content) != before['sha256']:
                    raise CompilationError('Uploaded file changed during compilation; retry: ' + path)


def sync_documents(client, project_id, paths, output_dir, timeout, *, state_file, force, write):
    paths = [client.validate_tex_path(path) for path in paths]
    if len(set(paths)) != len(paths):
        raise ValueError('Each TeX path must be specified only once.')
    pdf_paths = [PurePosixPath(path).with_suffix('.pdf') for path in paths]
    if len(set(pdf_paths)) != len(paths):
        raise ValueError('The selected TeX paths would overwrite the same output PDF.')
    try:
        state = load_state(state_file)
    except StateError as error:
        raise CompilationError(str(error)) from error
    server = client.base_url
    project_key = server + '/project/' + project_id
    previous = state['projects'].get(project_key, {})
    directory = output_dir / project_id
    built, reused, skipped, output_files = {}, {}, [], []
    client.refresh_csrf(project_id)
    with client.project_connection(project_id) as (socket, before):
        if before.get('rootDoc_id') is None:
            raise CompilationError('Cannot verify the saved main document: project metadata lacks rootDoc_id.')
        entries = client.project_entries(before)
        if any(project_path(path) != path for path in entries):
            raise CompilationError('The project contains an unsafe or noncanonical file path.')
        for path in paths:
            if path not in entries:
                raise CompilationError('TeX document not found in project: ' + path)
        structure = digest(encoded({path: {key: entry[key] for key in ('kind', 'id')} for path, entry in entries.items()}))
        settings = {key: before.get(key) for key in ('compiler', 'imageName')}
        sources = Sources(client, socket, project_id, entries, directory, previous.get('sources', {}), write)
        previous_builds = previous.get('builds', {})
        for root in paths:
            old = previous_builds.get(root, {})
            local_pdf = directory / PurePosixPath(root).with_suffix('.pdf')
            reasons = []
            if force:
                reasons.append('forced compilation')
            if not old:
                reasons.append('no previous successful build')
            elif old.get('settings') != settings:
                reasons.append('compiler settings changed')
            elif not local_pdf.is_file():
                reasons.append('cached PDF missing')
            elif digest(local_pdf.read_bytes()) != old.get('sha256'):
                reasons.append('cached PDF changed')
            inputs = old.get('inputs', {})
            if old and root not in inputs:
                reasons.append('missing dependency records')
            if not reasons:
                removed = set(inputs) - set(entries)
                changed = {path for path in inputs if path in entries
                           and sources.get(path)['sha256'] != inputs[path].get('sha256')}
                if removed:
                    reasons.append(changed_paths('input removed', removed))
                if changed:
                    reasons.append(changed_paths('input content changed', changed))
            if not reasons and old.get('conservative_project_scan'):
                # Some unresolved references were found only through recorder
                # inputs. Recheck those sources too, or a still-needed fallback
                # would look refinable and trigger a rebuild on every run.
                uncertain_sources = {path for path in inputs if any(
                    reference.startswith(path + ': ') for reference in old.get('uncertain_references', []))}
                dependencies, uncertain = sources.graph(root, uncertain_sources)
                if (not uncertain and not any(path in entries for path in ('latexmkrc', '.latexmkrc'))
                        and dependencies < set(inputs)):
                    # Learn actual recorder inputs once after replacing an old
                    # whole-project fallback with precise source discovery.
                    reasons.append('dependency discovery refined; refreshing compiler inputs')
            if not reasons and old.get('structure') != structure:
                # A new local package/graphic can change filename resolution.
                # Reparse using the new tree, retaining known runtime inputs.
                # An unrelated rename/addition/removal does not rebuild PDFs.
                dependencies, _ = sources.graph(root, inputs)
                if dependencies != set(inputs):
                    reasons.append(changed_paths('new dependency', dependencies - set(inputs)))
            if not reasons:
                skipped.append(root)
                reused[root] = {**old, 'structure': structure, 'root_doc_id': entries[root]['id'],
                                'inputs': {path: sources.records[path] for path in inputs}}
                report('Unchanged ' + root + '; using cached PDF.')
                continue

            report('Rebuilding ' + root + ': ' + '; '.join(reasons) + '.')
            # Reparse on every affected build so added/removed references update
            # the graph. Old runtime inputs seed checking, but are not retained
            # automatically in the final graph.
            dependencies, uncertain = sources.graph(root)
            watched = dependencies | (set(old.get('inputs', {})) & set(entries))
            for path in sorted(watched):
                sources.get(path)
            for attempt in range(2):
                report('Compiling ' + root + ' ...')
                result = client.compile_pdf(project_id, root, before, timeout, with_dependencies=True)
                runtime = recorder_inputs(result['artifacts'], root, entries)
                actual, runtime_uncertain = sources.graph(root, runtime)
                uncertain = sorted(set(uncertain + runtime_uncertain))
                if not actual <= watched:
                    # Newly observed runtime inputs were not snapshotted before
                    # this build. Build once more with their revisions known.
                    if attempt:
                        raise CompilationError('Dependencies changed during compilation; retry: ' + root)
                    watched.update(actual)
                    report('Discovered additional compiler inputs; verifying with a new build.')
                    continue
                sources.verify(actual)
                break
            pdf_path = str(PurePosixPath(root).with_suffix('.pdf'))
            record = {
                'tex': root, 'root_doc_id': entries[root]['id'], 'pdf': pdf_path,
                'sha256': digest(result['content']),
                'bytes': len(result['content']), 'inputs': {path: sources.records[path] for path in sorted(actual)},
                'structure': structure, 'settings': settings,
                'discovery': 'source+recorder' if result['artifacts'] else 'source',
                'conservative_project_scan': bool(uncertain) or any(path in entries for path in ('latexmkrc', '.latexmkrc')),
                'uncertain_references': uncertain,
            }
            built[root] = record
            output_files.append((directory / pdf_path, result['content']))

        after = client.get_project_infos(project_id)
        if after.get('rootDoc_id') != before['rootDoc_id']:
            raise CompilationError('The saved main document changed during the run; no new PDFs saved.')
        if client.project_entries(after) != entries or any(after.get(key) != before.get(key) for key in ('compiler', 'imageName')):
            raise CompilationError('Project structure or settings changed during the run; retry.')
        # Include cached outputs in the final check: an input may change while
        # another document is compiling.
        if built:
            all_inputs = set().union(*(set((built.get(root) or reused[root])['inputs']) for root in paths))
            sources.verify(all_inputs)
        merged_builds = {**previous_builds, **reused, **built}
        result = {
            'project_id': project_id, 'main_document_before': before['rootDoc_id'],
            'main_document_after': after['rootDoc_id'],
            'compiled': list(built), 'skipped': skipped,
            'files': [merged_builds[root] for root in paths],
        }
        for destination, content in output_files:
            write(destination, content)
        state['projects'][project_key] = {
            'sources': {**previous.get('sources', {}), **sources.records}, 'builds': merged_builds,
        }
        save_state(state_file, state, write=write)
        report('{} compiled, {} unchanged. Saved main document unchanged.'.format(len(built), len(skipped)))
        return result


def compile_documents(client, project_id, paths, output_dir, timeout, *, state_file=None, force=False):
    """Fetch changed PDFs using the default cache-state format."""
    return sync_documents(
        client, project_id, paths, output_dir, timeout,
        state_file=state_file if state_file is not None else DEFAULT_STATE_FILE,
        force=force, write=atomic_write,
    )
