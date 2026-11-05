# Runbook: box1 legacy ERP appliance

Commands run as root on the VM through administrative SSH (#25). `box1-erp` is on `PATH` (`/usr/bin/box1-erp`); application commands run as the application user: `runuser -u box1erp -- box1-erp …`.

Logs go to journald: `journalctl -u box1-erp-worker -u 'box1-erp-tui@*'`. They are JSON lines with millisecond UTC timestamps, request IDs, file names, `file_id`, `correlation_id` and `traceparent`. They never contain request content beyond IDs.

## Trust model

The console starts without a login on the VM console. Anyone who can open the VM's serial or VNC console can ship, reject, hold and switch modes. The operator name typed at the console is an audit label, not an authenticated identity. Console access is controlled by OpenShift RBAC on `virtualmachineinstances/console` and `vnc` (#25). The SFTP account reaches only `inbound/`, `outbound/` and `consumed/`.

## Health and monitoring

`runuser -u box1erp -- box1-erp status` prints JSON: mode, worker state and heartbeat age, counts, `oldest_pending_age_s`, `oldest_unpublished_age_s`, `oldest_quarantine_age_s` and recent failures. With `--check` it exits **0** healthy, **1** needs attention (quarantined files or failing publication), **2** worker down. The console shows the same on its first three lines:

| Console shows | Meaning | Action |
|---|---|---|
| `WAITING FOR OPERATOR` | MANUAL mode with pending requests. Healthy | Ship, hold or reject in the console |
| `PUBLICATION PAUSED (demo)`, `AUTO DELAYED (demo)`, `CRASH ARMED (demo)` | A demo fault is active | Console `X`; audited |
| `WORKER STOPPED` / `WORKER UNRESPONSIVE` | No fresh heartbeat. Imports and publication have stopped; the ledger shown is current but nothing new arrives | See "Worker" below |
| `PUBLICATION FAILURE`, `OUT` column `FAIL` | A result cannot be written to `outbound/` | Check the volume, free space and `ls -la /srv/box1-erp/exchange/outbound`; a directory planted at a result name blocks only that result. The worker retries with backoff up to 5 minutes |
| `Quarantine N` | Files the ERP did not accept; each has an `ERR` file for the adapter | Console `Q` |

## Worker

- Restart: `systemctl restart box1-erp-worker`. The console stays up and marks the worker stopped meanwhile.
- Restarts back off from 5 s to 5 min with no hard limit. A worker that hangs is killed by the systemd watchdog (60 s) and restarted.
- **Exit status 2 means do not restart**: the ledger, a required directory or the data volume is missing, or the schema needs `migrate`. The worker never creates a ledger, so a missing volume cannot silently produce an empty one. Check `findmnt /srv/box1-erp` and `journalctl -u box1-erp-worker -n 20`, fix the cause, then `systemctl reset-failed box1-erp-worker && systemctl start box1-erp-worker`.

## Console

- Restart: `systemctl restart box1-erp-tui@ttyS0` (or the selected TTY). The worker is unaffected.
- The console has no exit key and ignores Ctrl-C, Ctrl-Z and Ctrl-\. Exiting or crashing returns to the console after 2 s, backing off to 1 min if it keeps failing. It is never left stopped for good.
- **Broken console** (blank, or restarting): log in through administrative SSH, read `journalctl -u box1-erp-tui@ttyS0 -n 30`, fix the cause (config readable by `box1erp`? `TERM` correct for the console?), then `systemctl restart box1-erp-tui@ttyS0`. As a last resort, `systemctl stop box1-erp-tui@ttyS0 && systemctl start serial-getty@ttyS0` gives a login prompt on that console; return with `systemctl stop serial-getty@ttyS0 && systemctl start box1-erp-tui@ttyS0`.

## Quarantine and replay

Malformed, conflicting, oversized or unreadable inbound entries go to `/srv/box1-erp/state/spool/quarantine/` with a reason, and an `ERR` file tells the adapter. Unreadable entries (links, directories, files the ERP may not read) are moved unopened (`unread.*`). Console `Q` lists them; `P` replays one. Replay re-reads the stored copy through normal validation and deduplication, so a conflicting request stays quarantined; a replay that still fails returns the file to quarantine with a new `ERR`. A successful replay moves the file to the archive.

## Re-emit a result

Console `E` on a request republishes its latest result with the same result ID, revision and bytes. It never creates a shipment.

## Backup

```sh
runuser -u box1erp -- box1-erp backup            # online; /srv/box1-erp/state/backups/<UTC>/
runuser -u box1erp -- box1-erp verify-backup /srv/box1-erp/state/backups/<UTC>
```

The ledger is copied with SQLite's online backup API and integrity-checked; the spool and unconsumed exchange files go into `spool.tar` with row counts and sha256 digests. The backup appears only when complete. Copy it off the VM (#25 decides where): a backup on the same volume does not survive losing the volume.

## Restore

```sh
systemctl stop box1-erp-worker 'box1-erp-tui@*'
runuser -u box1erp -- box1-erp restore /srv/box1-erp/state/backups/<UTC>
systemctl start box1-erp-worker box1-erp-tui@ttyS0
```

- Restore is refused while the worker or any console has the ledger open (it needs an exclusive lock), and while a backup's digests, integrity or counts do not check out.
- The current ledger is kept as `state/ledger.pre-restore-<UTC>.db` and the spool as `spool/archive.pre-restore-<UTC>` and `spool/quarantine.pre-restore-<UTC>`. Nothing is deleted.
- The restored ledger starts in **MANUAL** mode, and the restore is audited.
- **Restoring to an older point is refused** if the current ledger has results or shipments the backup lacks, because the adapter may already hold them: shipping such a request again would be a second shipment, and new results would reuse revision numbers the adapter has seen (it quarantines them). The refusal lists the requests. Reconcile them with the adapter team first, then re-run with `--accept-divergence`; the listed requests are recorded in the audit trail and the previous ledger stays available for reference.
- When the ledger itself was lost there is nothing to compare: treat every request the adapter shows as further along than the restored ledger as divergent, and do not ship them again.

## Upgrade the application

The image (#25) carries a new wheel. The data volume stays. If the new version changes the ledger schema, the worker exits 2 with "run `box1-erp migrate`":

```sh
runuser -u box1erp -- box1-erp backup
systemctl stop box1-erp-worker 'box1-erp-tui@*'
runuser -u box1erp -- box1-erp migrate             # refuses while anything has the ledger open
systemctl start box1-erp-worker box1-erp-tui@ttyS0
```

A ledger newer than the code is refused rather than opened. Roll back by booting the previous image and restoring the backup taken before `migrate`.

## Rebuild the guest, keep the data volume

The user and group IDs are fixed (`box1erp` 962, `erpsftp` 963, `erpxfer` 961), so a rebuilt image owns the files on the kept volume. After the rebuild, run the provisioning script **without** `--init-ledger` (it only fixes ownership and modes), restore the SSH host keys if #25 keeps them on the volume, and start the services.

## Demo faults

Set `faults = true` in `/etc/box1-erp/erp.conf` (root) and restart both services. Turn it off after the demonstration. With it off, the worker clears any stored fault settings at start (audited), so re-enabling demo mode never re-arms an old fault.
