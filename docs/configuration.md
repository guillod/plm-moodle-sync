# Configuration and command reference

For installation and a first synchronization, see the [quick start](../README.md#quick-start).
[examples/sync.yaml](../examples/sync.yaml) is the complete, annotated example.
All IDs, file paths, and destinations in that example must be adapted to your setup.

A YAML file describes one PLMlatex project and one Moodle course. Project and
course IDs apply to every file in that configuration. Use separate YAML files
for other project/course pairs.

## Servers and local paths

| Setting | Meaning or default |
| --- | --- |
| `plmlatex.server` | HTTPS base URL; defaults to `https://plmlatex.math.cnrs.fr`. |
| `plmlatex.project_id` | Quoted 24-character hexadecimal ID from the project's `/project/…` URL. Required when files are configured. |
| `plmlatex.cookie_file` | Saved session; defaults to `.secrets/plmlatex.json`. |
| `moodle.server` | HTTPS base URL; defaults to `https://moodle-sciences-26.sorbonne-universite.fr`. Set it explicitly for your institution. |
| `moodle.course_id` | Positive integer from the course's `/course/view.php?id=…` URL. Required when any file has a Moodle target. |
| `moodle.cookie_file` | Saved session; defaults to `.secrets/moodle.json`. |
| `sync.cache_dir` | Sources and compiled PDFs; defaults to `.cache/files`. |
| `sync.state_file` | Build and publication metadata; defaults to `.cache/sync.json`. |
| `sync.log_file` | Persistent run log; defaults to `.cache/sync.log`. |

Server URLs may include an installation subdirectory, but no credentials, query,
or fragment. Both services use browser sessions; a Moodle web-service token is
not needed.

Local paths, including omitted defaults, are relative to the YAML file's
directory. Absolute paths stay absolute, and `~` expands to the user's home
directory. For example, `/courses/course.yaml` uses `/courses/.cache/sync.json`
unless overridden. Paths supplied through CLI flags are instead relative to
the current working directory.

The entire `sync` mapping is optional. Login-only configurations may omit IDs
and `files`; fetch-only configurations may omit the Moodle course.
Unknown options, duplicate YAML keys, invalid IDs, and conflicting paths are
rejected before network access. Omit optional fields instead of setting them to
`null`. Directories are created when data is saved, not when YAML is loaded.

### Sessions

Both login commands open the same Qt browser interface, then validate the
captured session before saving it. Session files are JSON objects with `server`
and `cookies` fields and owner-only permissions. Keep the complete captured
cookie set; rerun the corresponding login command to renew it. Session files
must match the configured server. PLMlatex CSRF tokens are obtained when needed.
To create these files using an existing Firefox or Chrome session, follow the
[manual login guide](manual-login.md).

For ENS/PSL, set `moodle.server: https://moodle.psl.eu`. Moodle sign-in also
recognizes an authenticated home or course page when the site redirects away
from `/my/`. Use a separate `moodle.cookie_file` for each Moodle server.

Keep sessions private and outside version control. `.secrets/` is ignored by
this repository; exclude any custom session paths as well.

## File mappings

`files` is a list of mappings. Each entry has a `source` mapping and may have a
`target` mapping. Omitting `target` fetches and caches the PDF without publishing
it. Per-file project and course IDs are not supported.

| Field | Meaning |
| --- | --- |
| `source.tex` | Required, case-sensitive TeX path inside the PLMlatex project, such as `worksheets/sheet1.tex`. |
| `target.section` | Required for publication: section name string or positive integer section database ID. |
| `target.name` | Required for publication: name displayed in Moodle. |
| `target.filename` | Optional downloadable filename; defaults to the TeX basename with `.pdf`, such as `sheet1.pdf`. |
| `target.visible` | Optional boolean enforcing visibility; omission preserves the existing setting. |
| `target.pages` | Optional quoted page or inclusive range; omission publishes the complete PDF. |

Source paths must be project-relative, with no parent-directory traversal.
Compiled PDFs follow the same directory structure in the cache, irrespective
of their Moodle names.

### Sections and names

A string such as `section: Worksheets` matches the exact displayed section
name. An integer such as `section: 1234` selects its database ID, not its
position in the course. A quoted number such as `section: "1234"` is a name.
Use `moodle-check` to list each section's `id`, `number` (position), and `name`.
Missing or ambiguous sections cause an error.

The display name and downloadable filename are independent. Setting
`name: Worksheet 1` does not change the default `sheet1.pdf`. An explicit
filename must end in `.pdf` and have no directory component. Changing only the
display name updates the same resource, preserving its ID and URL.

Within one section, each configured filename and display name must be unique.
Names must contain visible text and are compared after normalizing HTML and
whitespace. Duplicates using the same section selector are rejected during YAML
loading; aliases that select a section once by name and once by ID are checked
after resolving the course.

The same source can appear in several mappings with distinct destinations,
for example to publish several chapters. It is fetched once per run.

### Visibility

| Setting | Behavior |
| --- | --- |
| `visible: true` | Ensure the resource is shown on the course page. |
| `visible: false` | Ensure the resource is hidden from students. |
| Omitted | Preserve the current setting; use Moodle's form default for a new resource. |

Use unquoted YAML booleans. An explicit setting is checked on each upload and
applied even when the PDF is unchanged. Removing the option stops enforcing it.
Section visibility and other Moodle access restrictions still apply.

### Page selection

Use a quoted string such as `pages: "1-11"` or `pages: "3"`. Numbers refer to
physical positions in the PDF, starting at 1, including cover and contents
pages. Printed page numbers and PDF page labels do not determine the selection.
Descending, open-ended, and comma-separated ranges are not supported.

pypdf extracts the selected pages in memory, retaining text and vector content;
the cached PDF stays complete. Selections are validated before connecting to
Moodle, including during dry runs. A range beyond the last page fails rather
than producing a shorter PDF.

Changing or removing `pages` updates the same Moodle resource using the cached
complete PDF; it does not require recompilation.

## Commands

Use `python -m plm_moodle_sync COMMAND --config course.yaml`.
The installed `plm_moodle_sync` entry point provides the same commands.

| Command | Action |
| --- | --- |
| `plmlatex-login` | Create or renew the PLMlatex browser session. |
| `moodle-login` | Create or renew the Moodle browser session. |
| `moodle-check` | Inspect course sections and File forms without publishing. |
| `fetch` | Check sources, compile changed documents, and save PDFs locally. |
| `upload` | Publish verified cached PDFs without contacting PLMlatex. |
| `sync` | Fetch, then publish files with configured Moodle targets. |

### Previewing and forcing updates

`upload --dry-run` validates cached PDFs and page selections, inspects Moodle,
and reports planned actions. It writes the run log but does not upload PDFs,
submit course forms, or write synchronization state. Run `fetch` first if you
need to populate or refresh the cache. Opening Moodle edit forms during checks
may prepare temporary private draft files, as in a normal browser.

`fetch --force` recompiles even unchanged documents. `upload --force` and
`sync --force` replace even identical PDFs, preserving resource IDs; they do
not bypass checks for conflicting remote changes. To force compilation and
publication, run `fetch --force` followed by `upload --force`.

### Selection and overrides

| Option | Available on | Purpose |
| --- | --- | --- |
| `--tex PATH` | `fetch` | Select a configured source; may be repeated. |
| `--list-documents` | `fetch` | List all TeX paths in the configured project without compiling. |
| `--server`, `--cookie-file` | `fetch`, both login commands, `moodle-check` | Override the relevant service settings. |
| `--output-dir`, `--state-file` | `fetch` | Override cache and state paths. |
| `--course-id`, `--section-name` | `moodle-check` | Override the course or inspect one exact section name. |
| `--log-file` | `fetch`, `upload`, `sync` | Override the persistent log path. |
| `--timeout` | All commands | Positive request timeout in seconds: default 180 for `fetch`, `upload`, `sync`; 30 otherwise. |

`--list-documents` requires a project ID but no file mappings, and cannot be
combined with `--tex`. Without YAML, `fetch` accepts `--project-id` or an exact,
unique `--project-name`, together with `--tex` or `--list-documents`.

By default, `moodle-check` verifies configured sections and inspects an add-File
form and one existing File's edit form, when available. It prints JSON without
cookie values or session keys. Use each command's `--help` for its full syntax.

### Multiple configurations

```bash
python -m plm_moodle_sync sync --config course-a.yaml course-b.yaml
```

All commands accept multiple configurations. Repeating `--config` and shell
globs also work; duplicate resolved YAML paths run only once. CLI overrides
apply to every job. For `fetch --tex`, each selected path must exist in every
supplied configuration.

All YAML files and fetch selections are validated before the first job starts.
Jobs run sequentially. A runtime failure does not stop later jobs or undo earlier
successes; interruption stops the batch. Exit codes are 0 for success, 1 for
configuration/runtime failures, 2 for invalid CLI arguments, and 130 for an
interrupted run.

YAML files in the same directory share default sessions, cache, state, and log.
Project and course IDs distinguish their state entries. Configurations in other
directories use separate defaults unless given shared paths. Keep Moodle
destinations distinct across jobs: destination conflict checks run within each
job, not across the entire batch.

## Cache and publication state

The default layout is:

```text
.secrets/
  plmlatex.json
  moodle.json
.cache/
  sync.json
  sync.log
  files/
    PROJECT_ID/
      worksheets/
        sheet1.tex
        sheet1.pdf
        preamble.tex
```

### Incremental compilation

Sources, graphics, and generated PDFs share project folders. Revisions identify
sources to download; content hashes determine whether their PDFs need rebuilding.
The scanner resolves TeX inputs, local packages, graphics, bibliography files,
and simple file-wrapper macros. Compiler recorder inputs supplement discovery.
Unresolved dependencies conservatively track all project files.

Compilation selects a TeX root for that request, preserving the project's saved
main document. Unrelated file renames or revision changes with identical content
do not invalidate a resolved build. Added or removed files cause dependencies to
be checked again. Changes to tools or system files invisible to project metadata
may require `fetch --force`. Every rebuild logs its reason.

`sync.json` stores dependency metadata, build hashes, and publication records.
Keep it with the cache to reuse builds when relocating them. If an uploaded
source PDF shares a compiled PDF's path, it receives a separate cached filename,
such as `sheet1.source.pdf`.

### Moodle updates and recovery

Publication state records the Moodle course-module ID (`cmid`) and hash of each
uploaded PDF, including page selections. Updates preserve resource IDs, URLs,
and unrelated form settings. Switching a section selector between its name and
ID reuses the same record. Selecting by ID also survives a section-name change.

Before publishing, each job checks all its destinations and verifies remote
filenames and content against the saved state. Conflicting manual changes,
extra attachments, and modified cached PDFs stop publication. If a matching
resource has a different filename, the error reports both names for review.

Successful uploads are recorded individually after downloading and verifying
the result. Pending-save information supports recovery from interruptions.
Retry with `upload` to reuse cached PDFs after a publication failure. Keep the
state file: without it, only an existing PDF identical to the desired output
can be adopted automatically.

Removing a mapping does not delete its Moodle resource. Changing a course,
section, or filename does not move, rename, or delete the previous resource.
A remotely deleted resource is recreated on the next run.

An adjacent state lock rejects overlapping writers using the same state file.
It cannot prevent simultaneous manual edits in Moodle; changes detected before
submission stop publication. Dry runs do not acquire the state lock.

## Logs and scheduling

`fetch`, `upload`, and `sync` append to `sync.log_file`, including dry runs.
Entries use the machine's local time zone, with an explicit UTC offset, such as
`2026-09-17T15:00:00+0200`. They include run IDs, progress, rebuild reasons,
destinations, and outcomes. Logs rotate at 5 MiB with three backups and
owner-only permissions; an adjacent lock coordinates logging across processes.

Keep logs outside `sync.cache_dir`. Log, backup, and lock paths must not overlap
configuration, session, or state files. Default `.cache/` paths are ignored by
Git; exclude custom paths too. Deleting logs does not reset synchronization.

Network errors are sanitized; cookies, passwords, raw HTTP exchanges, and
arbitrary third-party output are not captured. An unwritable log stops the job.
Configuration errors and log setup failures go to stderr. Login commands and
`moodle-check` use console-only output.

The [systemd examples](../examples/systemd/README.md) provide a user service and
timer for hourly runs from 07:00 through 19:00, plus 01:00. Install customized
copies outside the repository in `~/.config/systemd/user/`.
Sessions may expire according to the server's settings; rerun the relevant
login command when authentication fails. Scheduled activity does not guarantee
indefinite session renewal.
