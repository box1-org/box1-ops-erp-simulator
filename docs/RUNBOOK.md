# Runbook: box1 legacy ERP appliance

Logs go to journald: `journalctl -u box1-erp-worker -u 'box1-erp-tui@*'`. They are JSON lines with request IDs, file names, correlation IDs and `traceparent`; they never contain request content beyond IDs.

## Health at a glance

`box1-erp status` (as `box1erp`) prints mode, worker state and heartbeat age, counts and conditions. The console shows the same on its first three lines:

| Console shows | Meaning | Action |
|---|---|---|
| `WAITING FOR OPERATOR` | MANUAL mode with pending requests. Healthy | Ship, hold or reject in the console |
| `PUBLICATION PAUSED (demo)`, `AUTO DELAYED (demo)`, `CRASH ARMED (demo)` | A demo fault is active | Console `X` to change; audited |
| `WORKER STOPPED` / `WORKER UNRESPONSIVE` | No fresh heartbeat. Imports and publication have stopped; the ledger shown is current but nothing new arrives | Restart the worker (below) |
| `PUBLICATION FAILURE` | A result could not be written to `outbound/` | Check the exchange mount and permissions; the worker retries every cycle |

## Restart independently

- Worker: `systemctl restart box1-erp-worker`. The console stays up and marks the worker stopped meanwhile.
- Console: `systemctl restart box1-erp-tui@ttyS0` (or the selected TTY). The worker is unaffected.
- The console has no exit key. Exiting or crashing returns to the console after 2 s; more than 10 starts a minute stops it (`systemctl reset-failed box1-erp-tui@ttyS0` then start).

## Worker will not start (exit 2)

Exit status 2 means the ledger or a required directory is missing. The worker never creates a ledger, so a missing data volume cannot silently produce an empty one. Check `findmnt /var/lib/box1-erp /srv/box1-erp`. Run `box1-erp init` **only** when provisioning a new appliance.

## Quarantine and replay

Malformed or conflicting inbound files go to `/var/lib/box1-erp/spool/quarantine/` with a reason, and an `ERR` file tells the adapter. Console `Q` lists them; `P` replays one. Replay re-reads the stored file through normal validation and deduplication, so a conflicting request stays quarantined.

## Re-emit a result

Console `E` on a request republishes its latest result with the same result ID, revision and bytes. It never creates a shipment.

## Backup and restore

```sh
runuser -u box1erp -- box1-erp backup                      # online; writes /var/lib/box1-erp/backups/<UTC>/
runuser -u box1erp -- box1-erp verify-backup <dir>
systemctl stop box1-erp-worker
runuser -u box1erp -- box1-erp restore <dir>               # refused while the worker heartbeat is fresh
systemctl start box1-erp-worker
```

The ledger is copied with SQLite's online backup API and integrity-checked; the spool and unconsumed exchange files go into `spool.tar` with row counts and sha256 digests. Restore replaces the ledger and the private archive and quarantine; it does not overwrite live `inbound/` or `outbound/`. After a restore to an older point, results published after the backup may be re-sent with the same IDs; the adapter treats them as duplicates or late results. Copy backups off the VM (#25 decides where).

## Demo faults

Set `faults = true` in `/etc/box1-erp/erp.conf` (root) and restart both services. Turn it off after the demonstration. With it off, stored fault settings are ignored.
