# Scheduled synchronization with systemd

These example **user units** run synchronization at 01:00 and every hour from
07:00 through 19:00 inclusive, every day in the machine's local time zone.
They require Linux with a systemd user manager.

- [plm-moodle-sync.service](plm-moodle-sync.service) runs one synchronization.
- [plm-moodle-sync.timer](plm-moodle-sync.timer) schedules the matching service.

## Configure and install

Complete the [quick start](../../README.md#quick-start) first, including login
and a successful manual sync. The scheduled service reuses the saved sessions.

From the repository directory, copy both examples to the user unit directory:

```bash
mkdir -p ~/.config/systemd/user
cp examples/systemd/plm-moodle-sync.service examples/systemd/plm-moodle-sync.timer \
  ~/.config/systemd/user/
```

Edit the installed `~/.config/systemd/user/plm-moodle-sync.service`:

- Set `WorkingDirectory` to the absolute project directory, without surrounding
  quotes; spaces in this value are allowed.
- Set the executable in `ExecStart` to the installed `plm_moodle_sync` command.
  Run `command -v plm_moodle_sync` in your activated environment to find it.
- Replace the YAML path with your configuration's absolute path. Add more
  quoted paths after `--config` to process several configurations.

In `ExecStart`, keep quotes around paths containing spaces. Systemd does not activate your
shell's virtual environment; the absolute executable path selects the correct
installation. YAML paths for sessions, cache, state, and logs remain relative
to the YAML file's directory.

Validate the customized copies and load them:

```bash
systemd-analyze --user verify ~/.config/systemd/user/plm-moodle-sync.service \
  ~/.config/systemd/user/plm-moodle-sync.timer
systemctl --user daemon-reload
```

Run the service once to check the configured paths and authentication. This
performs a real sync and may publish files:

```bash
systemctl --user start plm-moodle-sync.service
systemctl --user status plm-moodle-sync.service
```

A successful `oneshot` service returns to the inactive state after finishing.
Then enable the timer:

```bash
systemctl --user enable --now plm-moodle-sync.timer
systemctl --user list-timers --all plm-moodle-sync.timer
```

Enable the timer only; it starts the service when due. Customize the installed
copies so that repository updates do not overwrite your paths or schedule.

## Schedule and availability

The timer has two `OnCalendar` entries and a one-second accuracy window. If the
service is still running at the next trigger, systemd leaves that run in progress
instead of launching another instance. `Persistent=false` skips events missed
while the timer was stopped. The timer does not wake a sleeping machine;
calendar events missed during suspension can trigger once on resume.
See [systemd.timer](https://raw.githubusercontent.com/systemd/systemd/main/man/systemd.timer.xml).

To keep the user manager running after logout and start it at boot, enable
lingering for your account; this may require administrator authorization:

```bash
loginctl enable-linger "$USER"
```

See [loginctl](https://raw.githubusercontent.com/systemd/systemd/main/man/loginctl.xml)
for the account-lifetime behavior. Sessions can still expire: renew them with
the login commands using the same YAML file. A failed sync is recorded in the
logs, and the timer tries again at its next scheduled time.

## Logs and maintenance

Read the service output and latest result with:

```bash
journalctl --user -u plm-moodle-sync.service -n 100 --no-pager
systemctl --user status plm-moodle-sync.service plm-moodle-sync.timer
```

Application logs also follow the YAML's `sync.log_file` setting; see
[logging configuration](../../docs/configuration.md#logs-and-scheduling).

After editing either installed unit, reload the definitions and restart the
timer to apply schedule changes:

```bash
systemctl --user daemon-reload
systemctl --user restart plm-moodle-sync.timer
```

To stop future scheduled runs:

```bash
systemctl --user disable --now plm-moodle-sync.timer
```

Disabling the timer allows any synchronization already running to finish.
