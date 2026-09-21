"""Discover project-local inputs without executing TeX.

Literal references are followed recursively by the caller. Macro-built paths
are reported as uncertain so the caller can conservatively watch the project.
Compiler recorder files supplement this scan with inputs actually opened.
"""

import posixpath
import re
from pathlib import PurePosixPath


COMMAND = re.compile(r'\\(input|include|subfile|import|subimport|includefrom|subincludefrom|'
                     r'InputIfFileExists|IfFileExists|includegraphics|includepdf|'
                     r'bibliography|addbibresource|bibliographystyle|documentclass|'
                     r'LoadClass|usepackage|RequirePackage|lstinputlisting|verbatiminput)\b\*?')
TEXT_SUFFIXES = {'.tex', '.sty', '.cls', '.bib', '.bst', '.ltx', '.cfg', '.def'}
DEFINITION = re.compile(r'\\(?:newcommand|renewcommand|providecommand)\b\*?\s*'
                        r'(?:\{\\([A-Za-z@]+)\}|\\([A-Za-z@]+))')


def clean_tex(text):
    text = re.sub(r'\\begin\{(verbatim\*?|lstlisting|minted)\}.*?\\end\{\1\}', '', text, flags=re.S)
    text = re.sub(r'\\verb\*?([^\w\s]).*?\1', '', text)
    lines = []
    for line in text.splitlines():
        for index, character in enumerate(line):
            if character == '%':
                preceding = len(line[:index]) - len(line[:index].rstrip('\\'))
                if preceding % 2 == 0:
                    line = line[:index]
                    break
        lines.append(line)
    return '\n'.join(lines)


def group(text, start, opening='{', closing='}'):
    while start < len(text) and text[start].isspace():
        start += 1
    if start >= len(text) or text[start] != opening:
        return None, start
    depth, end = 1, start + 1
    while end < len(text):
        if text[end] == opening:
            depth += 1
        elif text[end] == closing:
            depth -= 1
            if depth == 0:
                return text[start + 1:end], end + 1
        end += 1
    return None, end


def project_path(value):
    value = posixpath.normpath(value)
    if value.startswith('/') or value == '..' or value.startswith('../') or '\x00' in value:
        return None
    return value.removeprefix('./')


def expand_file_wrappers(text):
    """Resolve local one-argument file wrappers called with literal arguments.

    Keep all alternative definitions (including both branches of conditionals).
    Unsupported declarations or nonliteral calls retain the original template,
    so ordinary discovery still requests conservative project tracking.
    """
    definitions, unsupported = {}, set()
    for match in DEFINITION.finditer(text):
        name = match.group(1) or match.group(2)
        count, end = group(text, match.end(), '[', ']')
        body, end = group(text, end)
        if count != '1' or body is None:
            unsupported.add(name)
            continue
        definitions.setdefault(name, []).append((match.start(), end, body))
    replacements, expanded = [], []
    for name, variants in definitions.items():
        # Limit expansion to file wrappers and simple formatting. Definitions,
        # recursion, conditional execution and arbitrary helper macros retain
        # the conservative fallback instead of being interpreted as TeX.
        commands = {command for _, _, body in variants for command in re.findall(r'\\([A-Za-z@]+)', body)}
        formatting = {'par', 'begingroup', 'endgroup', 'centering', 'tikzsetnextfilename',
                      'linewidth', 'textwidth', 'columnwidth'}
        if (name in unsupported or COMMAND.fullmatch('\\' + name)
                or any(command not in formatting and not COMMAND.fullmatch('\\' + command) for command in commands)
                or re.search(r'\\(?:def|gdef|edef|xdef|let)\s*\\' + re.escape(name) + r'\b', text)
                or not any(COMMAND.search(body) for _, _, body in variants)):
            continue
        # Ignore this macro's declarations when locating uses, but retain uses
        # inside other definitions: a # argument there is not statically known.
        calls = text
        for start, end, _ in reversed(variants):
            calls = calls[:start] + ' ' * (end - start) + calls[end:]
        arguments = []
        for call in re.finditer(r'\\' + re.escape(name) + r'\b', calls):
            value, _ = group(calls, call.end())
            if value is None or any(char in value for char in '\\#${}'):
                break
            arguments.append(value)
        else:
            if arguments:
                replacements.extend((start, end) for start, end, _ in variants)
                expanded.extend(body.replace('#1', value) for _, _, body in variants for value in arguments)
    for start, end in sorted(replacements, reverse=True):
        text = text[:start] + ' ' * (end - start) + text[end:]
    return text + '\n' + '\n'.join(expanded)


def discover(source, source_path, root_path, available):
    """Return (literal dependencies, uncertain references).

    Search both the compilation directory and the including file's directory.
    Keeping all matches is conservative for import packages and TEXINPUTS.
    """
    text = expand_file_wrappers(clean_tex(source))
    parents = list(dict.fromkeys([str(PurePosixPath(root_path).parent), str(PurePosixPath(source_path).parent), '.']))
    dependencies, uncertain = set(), []
    graphic_dirs = []
    for match in re.finditer(r'\\graphicspath\s*', text):
        value, _ = group(text, match.end())
        if value is not None:
            graphic_dirs.extend(re.findall(r'\{([^{}]+)\}', value))

    for match in COMMAND.finditer(text):
        command = match.group(1)
        options, end = group(text, match.end(), '[', ']')
        value, end = group(text, end)
        if value is None and command == 'input':
            bare = re.match(r'\s*([^\s{}\\]+)', text[end:])
            value = bare.group(1) if bare else None
        if command in ('import', 'subimport', 'includefrom', 'subincludefrom'):
            filename, end = group(text, end)
            value = posixpath.join(value, filename) if value is not None and filename is not None else None
        if command == 'documentclass' and value == 'subfiles' and options:
            value, command = options, 'input'
        if value is None or any(char in value for char in '\\#${}'):
            uncertain.append(command)
            continue
        if command in ('usepackage', 'RequirePackage'):
            extensions, system = ('.sty',), True
        elif command in ('documentclass', 'LoadClass'):
            extensions, system = ('.cls',), True
        elif command == 'bibliographystyle':
            extensions, system = ('.bst',), True
        elif command in ('bibliography', 'addbibresource'):
            extensions, system = ('.bib',), False
        elif command in ('includegraphics', 'includepdf'):
            extensions, system = ('.pdf', '.png', '.jpg', '.jpeg', '.eps', '.svg'), False
        else:
            extensions, system = ('.tex',), False
        for item in value.split(','):
            item = item.strip().strip('"')
            if not item:
                continue
            names = [item] if PurePosixPath(item).suffix else [item, *(item + ext for ext in extensions)]
            directories = list(parents)
            if command == 'includegraphics':
                directories.extend(posixpath.join(parent, directory) for parent in parents for directory in graphic_dirs)
            candidates = {project_path(posixpath.join(parent, name)) for parent in directories for name in names}
            matches = candidates & set(available)
            dependencies.update(matches)
            if not matches and not system and command not in ('IfFileExists', 'InputIfFileExists'):
                uncertain.append(command + ':' + item)
    return dependencies, uncertain


def recorder_inputs(artifacts, root_path, available):
    """Map .fls INPUT records and latexmk database entries into the project.

    System files are excluded; only paths present in the project are retained.
    Absolute build paths are resolved against the recorder's PWD.
    """
    found = set()
    root_parent = str(PurePosixPath(root_path).parent)
    for name, content in artifacts.items():
        text = content.decode('utf8', errors='replace')
        if name.endswith('.fls'):
            working_dirs = [line[4:] for line in text.splitlines() if line.startswith('PWD ')]
            values = [line[6:] for line in text.splitlines() if line.startswith('INPUT ')]
        else:
            working_dirs = []
            values = re.findall(r'^\s+"([^"]+)"\s', text, flags=re.M)
        for value in values:
            value = value.strip('"')
            candidates = [value, posixpath.join(root_parent, value)]
            if value.startswith('/'):
                candidates = []
                for directory in working_dirs:
                    relative = posixpath.relpath(value, directory)
                    candidates.extend([relative, posixpath.join(root_parent, relative)])
            for candidate in candidates:
                normalized = project_path(candidate)
                if normalized in available:
                    found.add(normalized)
    return found
