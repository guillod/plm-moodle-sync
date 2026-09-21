"""URL validation shared by both services."""

from urllib.parse import urlsplit


def normalize_server(server):
    server = server.rstrip('/')
    parsed = urlsplit(server)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or
            parsed.password or parsed.query or parsed.fragment):
        raise ValueError('The server must be an HTTPS URL without credentials, query, or fragment.')
    return server
