"""Parse Moodle page data without executing JavaScript."""

import json
import re
import unicodedata

from bs4 import BeautifulSoup


def visible_text(value):
    return unicodedata.normalize('NFC', ' '.join(
        BeautifulSoup(str(value), 'html.parser').get_text(' ', strip=True).split()))


def script_objects(html, pattern):
    """Decode embedded JSON without executing JavaScript."""
    for script in BeautifulSoup(html, 'html.parser').find_all('script'):
        source = script.string or script.get_text()
        for match in re.finditer(pattern, source):
            try:
                obj, _ = json.JSONDecoder().raw_decode(source[match.end():].lstrip())
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield obj
