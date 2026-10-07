# An Update runs in its own systemd unit, triggered by a request file

The app cannot perform an Update itself. Its unit runs with
`NoNewPrivileges` and `ProtectSystem=strict`, so `/opt` is read-only to it
and sudo refuses to run, and anything it forks dies when its own unit
restarts. So an Update runs in a separate oneshot
`sp-rtk-base-update.service`, which systemd starts outside the app's sandbox
and which therefore outlives the app's restart. The unit runs
`sp-rtk-base-apply-update`, a console script shipped in the package, as the
`sp-rtk-base` user that already owns the venv. That script works out the
newest stable release and the newest Relay its pin allows, and installs
exactly `sp-rtk-base==X sp-rtk-base-relay==Y`. The only root steps are fixed
`+/usr/bin/systemctl restart` lines for `sp-rtk-base.service` and
`try-restart` for `sp-rtk-base-net-provision.service`, followed by a health
check of the new version.

The app starts an Update by writing a request file to
`/var/lib/sp-rtk-base/update/`, which `sp-rtk-base-update.path` watches. The
request names the X and Y the operator read Release notes for, but only as
an expectation: the updater resolves the target itself and refuses if its
answer differs ("a newer release appeared; check again"). Nothing from the
file reaches pip, so an app that has been compromised can press Update but
cannot choose a version. Progress and outcome come back through
`/var/lib/sp-rtk-base/update/status.json`, written by the updater at each
phase. The service user cannot read the update unit's journal. If no phase
follows `requested` within 30 s, the Update did not start.

Update is turned off at the host, not in `config.yaml`: either
`install.sh --no-update` leaves the path unit disabled, or the admin disables
it. The app reads the unit's enabled state and says so, and keeps the check
and the Release notes. A switch in `config.yaml` would not hold, because the
app can write that file.

A new version that fails its health check is rolled back automatically.
Before pip runs, the updater copies the venv to `venv.prev` and
`/etc/sp-rtk-base/` to a snapshot, so the unit also needs write access to
`/etc/sp-rtk-base`. The verify step runs from `venv.prev`, the old version's
code, because a release broken badly enough to fail on import could not
judge or undo itself. On failure it restores the snapshot, writes a rollback
marker in `/var/lib/sp-rtk-base/update/` and exits non-zero. A fixed
`ExecStopPost=+` line then restarts the app, but only when the marker
exists. The app can write that directory, so the most a compromised app
gains by planting the marker is a restart of itself. A failure before the
restart (a pip error) restores the snapshot without restarting. A rollback
gets one attempt, never a loop.

Update never rewrites unit files, because that would make the updater a
root step. A release that needs changed host files instead raises an integer
plumbing version, kept in `deploy/plumbing-version`. `install.sh` writes it
into `sp-rtk-base-update.service` as `Environment=SP_RTK_BASE_PLUMBING=N`.
It lives in the unit file and not in a state file, because unit files are
root-only and the app could forge a file in a directory it owns. A host
without the unit counts as 0. Before an Update, the app fetches the target
tag's `deploy/plumbing-version`, and the updater checks it again while
resolving the target. If the host's number is lower, or the requirement
can't be read, Update is unavailable and the app shows the one-time
`install.sh` re-run instead. A base installed before Update gets the
plumbing the same way. `install.sh` fetches its unit files from the tag of
the version it installs, not `main`, so the recorded number always matches
the files beside it. CI fails when host files under `deploy/` change without
a bump.

## Considered options

- **`systemctl start` allowed by a polkit rule scoped to the unit and the
  `start` verb.** It gives immediate feedback, but `managed-host` installs
  would need polkitd added to a host they are not supposed to change. A rule
  that omits the `unit` check also authorizes `StartTransientUnit`, which
  carries no details, and that is root for the web app.
- **sudoers.** It cannot work: sudo detects `no_new_privs` and exits, and
  `ProtectKernelTunables`/`ProtectKernelModules` imply `NoNewPrivileges`
  anyway.
- **Exit non-zero and install from an `ExecStartPre=+` hook.** The UI is down
  for the whole pip run, and a root hook reads a file the app can write on
  every boot.
- **An updater script laid down by `install.sh` outside the venv.** Every fix
  to it would need `install.sh` re-run on every base. Shipped in the package,
  fixes arrive through Update itself. Only the unit files are frozen at
  install time.

## Consequences

- The **old** version's updater installs the new version. Changes to the
  request or status format must stay readable across one release.
- A release must never write config or profiles that the previous release
  cannot read, or a Rollback brings back a version that cannot start.
- The unit files and their root lines change only when `install.sh` is
  re-run. That includes `sp-rtk-base.service` itself, so a release that
  needs a different sandbox must raise the plumbing version.
