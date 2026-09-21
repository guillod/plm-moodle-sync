# PLMlatex → Moodle synchronization

Keep a Moodle course's PDFs up to date with the LaTeX sources in a PLMlatex
project. The tool compiles each configured TeX document separately, so lecture
notes, exercise sheets, and other documents can share a project. It publishes
each PDF as a Moodle File resource, either in full or with only a selected page
or page range.

On subsequent runs, the tool updates the same Moodle resources, so existing
links keep working. Their visibility settings are preserved unless you
explicitly override them in the configuration.

To avoid unnecessary compilation, the tool checks source revisions and
automatically identifies the files each document depends on, such as included
TeX files, images, and bibliographies. It reuses cached PDFs when those files
are unchanged.

Each YAML configuration specifies one PLMlatex project, one Moodle course,
and which documents to publish in which course sections. Several configurations
can be processed in one command to synchronize multiple projects and courses.

The integration was developed for CNRS PLMlatex and Sorbonne Université's Moodle.
Other installations may require adjustments to their authentication, editor
protocol, or Moodle forms.

## Installation

From the repository directory, using Python 3.10 or newer:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[login]'
```

The commands below assume this environment is active.

File locking requires Linux or macOS. Interactive login uses a Qt WebEngine
window and requires a graphical desktop. For a machine that only synchronizes
using saved sessions, install with:

```bash
python -m pip install -e .
```

## Quick start

1. Create your configuration:

   ```bash
   cp examples/sync.yaml course.yaml
   ```

   Edit the server URLs, project and course IDs, TeX paths, and Moodle targets.
   The example contains placeholder values. Your accounts need access to the
   PLMlatex project and permission to edit the Moodle course.

2. Sign in to both services and check the Moodle destinations:

   The login commands open institutional sign-in pages that may ask for your
   password; the application saves session cookies **locally** and
   **does not store your password**.
   Alternatively, [create the session files manually with Firefox or Chrome](docs/manual-login.md).

   ```bash
   python -m plm_moodle_sync plmlatex-login --config course.yaml
   python -m plm_moodle_sync moodle-login --config course.yaml
   python -m plm_moodle_sync moodle-check --config course.yaml
   ```

3. Compile the configured documents and preview publication:

   ```bash
   python -m plm_moodle_sync fetch --config course.yaml
   python -m plm_moodle_sync upload --config course.yaml --dry-run
   ```

4. Publish, then repeat this command whenever you want to synchronize:

   ```bash
   python -m plm_moodle_sync sync --config course.yaml
   ```

New files are added at the end of their Moodle section. You can then arrange them
within the section in Moodle, and their order will be preserved during future syncs.

Login saves sessions under `.secrets/`; keep these files private. The default
`.cache/` directory contains the PDFs, synchronization state, and logs. Retain
it between runs to reuse builds and track published resources.

## Scheduled synchronization with systemd

On Linux, the example [service](examples/systemd/plm-moodle-sync.service) runs
one synchronization using your saved sessions. The matching
[timer](examples/systemd/plm-moodle-sync.timer) schedules it daily at **01:00**
and **every hour from 07:00 through 19:00 inclusive**, in local time.

Install customized copies in `~/.config/systemd/user/`, outside the repository.
Set the project directory, executable, and YAML paths in the service before
enabling the timer. The [setup guide](examples/systemd/README.md) covers
installation, activation, logs, and running after logout.

## Documentation

See the [configuration and command reference](docs/configuration.md) for:

- [YAML settings and defaults](docs/configuration.md#servers-and-local-paths).
- [File names, sections, visibility, and page selections](docs/configuration.md#file-mappings).
- [Commands and multiple configurations](docs/configuration.md#commands).
- [Caching, publication, and retries](docs/configuration.md#cache-and-publication-state).
- [Logs and scheduling](docs/configuration.md#logs-and-scheduling).

The [example configuration](examples/sync.yaml) shows the complete structure.
Use `python -m plm_moodle_sync --help` in the activated environment for CLI help.
The installed `plm_moodle_sync` command provides the same interface.
Use `python -m plm_moodle_sync --version` (or `plm_moodle_sync --version`)
to display the package version.

## Development

The package version is derived from Git tags by
[`setuptools-scm`](https://setuptools-scm.readthedocs.io/en/stable/usage/)
when building or installing. Tag release commits as `vX.Y.Z`: a clean checkout
of `v0.1.0` builds version `0.1.0`. Commits after a release and uncommitted
changes produce development versions with Git information.

`__version__` and the CLI read the installed package metadata, so Git is not
needed at runtime. After changing commits or tags in an editable installation,
rerun `python -m pip install -e .` to refresh the version. An uninstalled source
checkout reports `0+unknown`.

Build from a Git checkout with its tags, or from a generated source distribution
that preserves version metadata.

Run the offline tests with the installed dependencies:

```bash
python -m unittest discover -s tests -v
```

Optional browser tests use synthetic pages and cookies, without institutional
sign-in:

```bash
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -p browser_smoke.py -v
```

Tests cover configuration, dependencies, caching, PDF extraction, publication,
authentication, and logs. Remote services are mocked in the offline suite.

## Credits

The code for this project was written by AI under the direction of Julien Guillod.

The PLMlatex client derives from `overleaf-sync-plm`. See
[upstream attribution and license](plm_moodle_sync/UPSTREAM.md).
Moodle form extraction uses `python-moodle`; PDF page extraction uses `pypdf`.

## License

This project is licensed under the [GNU General Public License, version 3](LICENSE)
(`GPL-3.0-only`). The original upstream MIT copyright and permission notice is
retained in [LICENSE.overleaf-sync](plm_moodle_sync/LICENSE.overleaf-sync).

This software is provided "as is", in the hope that it will be useful, but without any warranty.

## Contact

Please open an issue in this repository for questions, bug reports, or feature
requests. For other enquiries, contact:

- **Julien Guillod**, Department of Mathematics, Sorbonne University, France.
- Email: `julien.guillod [at] sorbonne-universite.fr`.
- Website: [guillod.org](https://guillod.org/).
