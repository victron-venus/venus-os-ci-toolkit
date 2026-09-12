# Local nightly checks

`scripts/run_nightly.py` runs `python scripts/release.py check` sequentially in
selected existing checkouts. The release client executes the reviewed
`local_checks` from each repository's `.release-policy.json`; every declared
check must pass. This uses your machine and does not require paid GitHub features
or GitHub Actions minutes. Electricity, installed tools and dependencies still
come from the local environment.

The runner does not fetch, pull, switch branches, reset files, create commits,
dispatch workflows, publish artifacts or deploy. Project check scripts are
trusted executable code: review their declared commands before scheduling them.
They may install locked development dependencies, download vulnerability
databases or start disposable local containers. Missing tools, unavailable Docker,
test failures and security findings fail the run; they are not silently skipped.
This is local validation, not CodeQL equivalence or evidence qualifying an RC
for stable publication. An enabled local scheduler does not enable hosted jobs.

## Prepare and run once

Use Python 3.11 or later, Git, and each selected project's documented check
toolchain. Use dedicated checkouts of reviewed source and a persistent report
directory outside those checkouts. The fleet inventory determines the immediate
directory names beneath `--root`; `--root` is required so a scheduler never guesses
which working copies to use. The checked-in policy and both origin URLs must match
the inventory repository identity. Check entrypoints must be tracked regular
files. Dirty working trees fail preflight and remain untouched.

For example, after replacing the absolute paths with your own:

```bash
/opt/homebrew/bin/python3 \
  /Users/you/ci-toolkit/scripts/run_nightly.py \
  --root /Users/you/nightly-checkouts \
  --logs /Users/you/Library/Logs/venus-nightly \
  --repo 4alvit/github-deploy-webhook \
  --timeout 3600
```

Repeat `--repo OWNER/REPO` to select additional projects. Omitting `--repo` checks
every active entry in the toolkit's `fleet.json`; unknown or explicitly excluded
selections are errors. `--inventory /absolute/path/fleet.json` can select another
reviewed inventory with the same schema. Entries must name distinct immediate
child directories; symlinked checkouts and escaping entrypoints are rejected.

The runner deliberately does not keep source current. Update each dedicated
checkout manually through your normal review process before expecting newer code
to be tested. Each result records its HEAD SHA, commit date, branch, dirty state,
and the locally cached `origin/<default_branch>` SHA when available. Even when
HEAD matches that cached ref, remote freshness remains **unknown**: no fetch or
GitHub request was performed. A green result may be for old source. The report
cannot establish that current GitHub HEAD or a release candidate passed.

Run once interactively and inspect the result before installing a schedule.
Use the same operating-system user and tool paths for the scheduled run. Do not
provide production credentials merely to run local validation.

## Reports and failures

Each invocation creates a private, unique UTC-named subdirectory of `--logs` with:

- `001-<directory>.log`, etc.: checkout metadata, combined check output and result.
- `results.jsonl`: one flushed result per completed repository.
- `summary.json`: atomically updated after each repository, plus final status.

The runner returns `0` only if every selected repository passes, `1` for failed,
timed-out or interrupted checks, and `2` for invalid configuration or an already
running invocation using the same log root. A failure does not skip later
repositories. Checks run serially. `--timeout` limits each check process group
(default 3600 seconds; maximum 86400); timeout terminates its child processes
before continuing. Use one log root for a schedule so the filesystem lock prevents
overlapping invocations.

The console reports the summary path at startup. If the machine stops abruptly,
the last durable summary may still say `running`; inspect completed JSONL entries
and the current repository's log rather than treating it as success. A process
terminated outside the runner's timeout/interrupt handling may need operator
cleanup of local test resources. No release is authorized by these logs.

Logs can contain dependency and test diagnostics. Keep the directory private;
the runner creates new report directories with mode `0700`, does not upload logs,
and does not remove older runs. Review disk use and apply your own retention policy.

## macOS launchd example

The following is an operator-installed **LaunchAgent**, running at 03:15 local
time. Replace every `/Users/you` path and the Python path; add selected repositories
as separate `--repo`/identity argument pairs. Create the stdout/stderr parent
directory first. Save the reviewed file as
`~/Library/LaunchAgents/local.venus.nightly.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.venus.nightly</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/python3</string>
    <string>/Users/you/ci-toolkit/scripts/run_nightly.py</string>
    <string>--root</string><string>/Users/you/nightly-checkouts</string>
    <string>--logs</string><string>/Users/you/Library/Logs/venus-nightly</string>
    <string>--repo</string><string>4alvit/github-deploy-webhook</string>
    <string>--timeout</string><string>3600</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>15</integer></dict>
  <key>StandardOutPath</key><string>/Users/you/Library/Logs/venus-nightly/launchd.out.log</string>
  <key>StandardErrorPath</key><string>/Users/you/Library/Logs/venus-nightly/launchd.err.log</string>
</dict>
</plist>
```

After the manual run passes, the operator can validate with `plutil -lint` and
register it with `launchctl bootstrap gui/$(id -u)
~/Library/LaunchAgents/local.venus.nightly.plist` on one shell command line.
To remove that schedule, use `launchctl bootout gui/$(id -u)
~/Library/LaunchAgents/local.venus.nightly.plist`, also on one line. This document
does not install or start it. Keep the machine available for the intended window
and verify actual run timestamps in the report directory.

## Linux systemd user timer example

Replace the paths and repository selection. Save these two reviewed files under
`~/.config/systemd/user/`. A user timer requires a running user service manager;
check the host's existing login/session policy before relying on overnight runs.

`venus-nightly.service`:

```ini
[Unit]
Description=Local checked-in fleet validation

[Service]
Type=oneshot
Environment=PATH=/home/you/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/bin/python3 /home/you/ci-toolkit/scripts/run_nightly.py --root /home/you/nightly-checkouts --logs /home/you/.local/state/venus-nightly --repo 4alvit/github-deploy-webhook --timeout 3600
TimeoutStartSec=infinity
UMask=0077
```

`venus-nightly.timer`:

```ini
[Unit]
Description=Nightly local fleet validation

[Timer]
OnCalendar=*-*-* 03:15:00
Persistent=true
Unit=venus-nightly.service

[Install]
WantedBy=timers.target
```

After a successful manual run, the operator can use `systemd-analyze --user verify`
on the service and timer files, then `systemctl --user daemon-reload` and
`systemctl --user enable --now venus-nightly.timer`. Inspect
`systemctl --user list-timers` and `journalctl --user -u venus-nightly.service` plus
the durable JSON summary. Disable the schedule with
`systemctl --user disable --now venus-nightly.timer`. These commands are
instructions only; the toolkit neither installs timers nor changes login policy.
