# Upstream attribution

The client and browser authentication code are derived from
[JulesColas97/overleaf-sync-plm](https://github.com/JulesColas97/overleaf-sync-plm),
commit `ad0e6edc0d2f0869a5ec1145a84e6c84c254b531`, itself a fork of
[moritzgloeckl/overleaf-sync](https://github.com/moritzgloeckl/overleaf-sync).

Original files: `olsync/olclient.py` and `olsync/olbrowserlogin.py`.
The original copyright and MIT license are retained in
[LICENSE.overleaf-sync](LICENSE.overleaf-sync).

## Adapted code

- [plmlatex/client.py](plmlatex/client.py) adapts project retrieval and
  compilation. Local changes add explicit TeX root selection, document revision
  checks, compiler dependency records, and compatibility fixes for the legacy
  Socket.IO protocol.
- [plmlatex/auth.py](plmlatex/auth.py), [common/login.py](common/login.py), and
  [common/browser.py](common/browser.py) adapt browser sign-in. Local changes
  separate service-specific login rules from the shared browser, validate
  captured sessions, and store cookies as JSON with owner-only permissions.

The upstream package is not a runtime dependency. This project is distributed
under GNU GPLv3 (`GPL-3.0-only`), with the original upstream MIT copyright and
permission notice retained. Third-party dependencies installed separately
retain their own licenses.
