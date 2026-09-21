"""Service-supplied browser settings and cookie collection, without Qt imports.

Cookie collection is adapted from overleaf-sync-plm. Copyright (c) 2021
Moritz Glöckl. See ../LICENSE.overleaf-sync and ../UPSTREAM.md.
"""

from collections.abc import Callable, Collection
from dataclasses import dataclass
from urllib.parse import urlsplit


class BrowserLoginError(RuntimeError):
    """An interactive browser could not be started."""


class CookieCapture:
    """Collect cookies applicable to a target URL, with an optional name filter."""

    def __init__(self, target_url, *, cookie_names=None):
        self.target = urlsplit(target_url)
        self.cookie_names = cookie_names
        self.values = {}

    def update(self, name, value, domain, path='/', *, removed=False):
        domain = domain.lower()
        host = self.target.hostname
        matches_host = host == domain or (
            domain.startswith('.') and
            (host == domain[1:] or host.endswith(domain))
        )
        path = path or '/'
        matches_path = self.target.path == path or (
            self.target.path.startswith(path) and
            (path.endswith('/') or self.target.path[len(path):].startswith('/'))
        )
        if (self.cookie_names is not None and name not in self.cookie_names) or not matches_host or not matches_path:
            return
        key = (name, domain, path)
        if removed:
            self.values.pop(key, None)
        else:
            self.values[key] = value

    def cookies(self):
        # Prefer the most specific cookie path if the browser holds two versions.
        return {key[0]: self.values[key] for key in sorted(self.values, key=lambda key: len(key[2]))}


@dataclass(frozen=True)
class LoginSpec:
    title: str
    start_url: str
    cookie_target: str
    cookie_names: Collection[str] | None
    is_dashboard: Callable[[str], bool]
    dashboard_script: str
    has_session: Callable[[dict[str, str]], bool]

    def capture(self):
        return CookieCapture(self.cookie_target, cookie_names=self.cookie_names)
