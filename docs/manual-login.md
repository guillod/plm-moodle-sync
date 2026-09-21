# Manual login with Firefox or Chrome

You can create the session files from your own signed-in browser instead of
running `plmlatex-login` and `moodle-login`. This is useful when the application's
Qt browser is unavailable. No browser extension or optional `login` dependency
is needed for this method.

The application stores session cookies, **not your password**. These cookies
allow access as your account while the session is valid, so keep the files
private and outside version control.

## 1. Sign in to each service

Use the server URLs from your YAML configuration:

| Service | Page to open after signing in |
| --- | --- |
| PLMlatex | `<plmlatex.server>/project` |
| Moodle | `<moodle.server>/my/` |

Complete institutional sign-in and any second-factor challenge in Firefox or
Chrome. Wait until you reach the service's authenticated dashboard. Collect
cookies from that service, after the return from institutional sign-in.

## 2. Read the cookies

### Firefox

Open **Developer Tools → Storage → Cookies** and select the service's origin.
The table shows each cookie's **Name**, **Value**, **Domain**, and **Path**.
Copy the complete value from the table; the details sidebar can show a parsed
representation instead of the original string. See Firefox's
[Storage Inspector](https://firefox-source-docs.mozilla.org/devtools-user/storage_inspector/index.html)
and [cookie table](https://firefox-source-docs.mozilla.org/devtools-user/storage_inspector/cookies/index.html).

### Chrome

Open **Developer Tools → Application → Storage → Cookies** and select the
service's origin. Select each cookie to view its full value. Leave **Show
URL-decoded** unchecked so that percent-encoded values are copied unchanged.
See [Chrome's cookie inspector](https://developer.chrome.com/docs/devtools/application/cookies).

### Which cookies to copy

Use cookies whose domain and path apply to the dashboard URL. A cookie may
belong to the exact host or an applicable parent domain; for an installation
under a subdirectory, check the cookie's path too. Do not copy cookies belonging
only to a separate institutional sign-in host or unrelated embedded site.

- **PLMlatex:** copy `sharelatex.sid` or `overleaf_session2`, whichever is present,
  plus `oauth.session` if present. If both session-cookie names are present,
  copy both. These are the names captured by the application's login flow.
- **Moodle:** copy **all cookies applicable to the Moodle dashboard**, including
  `MoodleSession` or its site-specific suffixed name. Other cookies may be needed
  by the institution's authentication system; do not reduce the set to just
  `MoodleSession`.

Copy names and raw values exactly, without decoding, truncating, or changing
them. If several rows share a name, check the dashboard request in the
**Network** panel to identify the value actually sent in its `Cookie` request
header; the JSON format has only one entry per name.

Use the developer-tools cookie table, not `document.cookie` in the JavaScript
console: authentication cookies marked **HttpOnly** are inaccessible to page
JavaScript. See [HttpOnly cookies](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Set-Cookie#httponly).

## 3. Create the JSON files

The paths below assume the default session settings. Run these commands from
the directory containing your YAML configuration:

```bash
mkdir -p .secrets
chmod 700 .secrets
```

Use a local text editor to create `.secrets/plmlatex.json` and
`.secrets/moodle.json`. These examples show the structure only: replace every
placeholder and add the other applicable cookies collected above. If your
browser uses a different session-cookie name, use that exact name.

**`.secrets/plmlatex.json`:**

```json
{
  "server": "https://plmlatex.math.cnrs.fr",
  "cookies": {
    "sharelatex.sid": "REPLACE_WITH_RAW_COOKIE_VALUE"
  }
}
```

**`.secrets/moodle.json`:**

```json
{
  "server": "https://moodle.example.edu",
  "cookies": {
    "MoodleSession": "REPLACE_WITH_RAW_COOKIE_VALUE"
  }
}
```

Set each `server` to the corresponding YAML server URL, without a trailing
slash. Preserve any installation subdirectory; do not append `/project`,
`/my/`, or a course URL. The Moodle hostname above is a placeholder.

`cookies` must be an object mapping names to string values, not a browser-export
array or a `cookies.txt` file. Include only name/value pairs, without domain,
expiry, or other attributes. Use valid JSON with double quotes, escaping any
literal quotes or backslashes inside values. No password, PLMlatex CSRF token,
or Moodle `sesskey` belongs in these files.

Restrict access to the saved files:

```bash
chmod 600 .secrets/plmlatex.json .secrets/moodle.json
```

The default paths are already used when `cookie_file` is omitted from YAML.
For custom paths, use the corresponding `plmlatex.cookie_file` and
`moodle.cookie_file` settings; paths are relative to the YAML file's directory.
The repository ignores `.secrets/`; exclude custom session locations from Git too.

## 4. Check the sessions

From your installed Python environment, use your configuration file to check
access without compiling or uploading PDFs:

```bash
python -m plm_moodle_sync fetch --config course.yaml --list-documents
python -m plm_moodle_sync moodle-check --config course.yaml
```

The first command lists TeX documents in the configured PLMlatex project. The
second checks the Moodle course and configured sections. Moodle checks can
prepare temporary private draft files when inspecting edit forms, as in a
normal browser.

After successful checks, continue with fetching and publication in the
[quick start](../README.md#quick-start); skip its two login commands because
you have already created the sessions.

## Renewal and troubleshooting

If authentication fails, confirm that the browser is still signed in, refresh
the dashboard, and copy the current cookie values again. Check the server URL,
raw encoding, and complete Moodle cookie set before retrying. Merely creating
valid JSON does not establish that a session is authenticated.

Logging out in the browser can invalidate the copied session. Server-side
expiry or cookie rotation can also require exporting it again. You can renew
the files through either this manual procedure or the application's login
commands, using the same configuration and session paths.
