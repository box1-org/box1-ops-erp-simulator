# VM integration contract for #25

What this repository provides, how #25 installs it, and what #25 must show in the VM to close #14's VM criteria. Questions this document does not answer are a gap in it: raise them on box1-docs#14.

## Artifact

`box1_ops_erp_simulator-<version>-py3-none-any.whl` plus `systemd/`, `packaging/`, `config/` and this document. Standard library only (Python 3.12, RHEL 10's system Python). Proposed delivery (D-002, needs approval): an OCI image `FROM scratch` holding these files and a `SHA256SUMS`, pushed to `box1-ci/box1-ops-erp-simulator@sha256:<digest>` and consumed in the bootc Containerfile with `COPY --from=<registry>/box1-ci/box1-ops-erp-simulator@sha256:<digest> / /tmp/box1-erp/`. Pin by digest.

## Install (bootc image build)

```sh
install -m0644 /tmp/box1-erp/packaging/sysusers-box1-erp.conf /usr/lib/sysusers.d/box1-erp.conf
systemd-sysusers                        # fixed IDs: box1erp 962, erpsftp 963, group erpxfer 961
python3 -m venv --system-site-packages /usr/lib/box1-erp/venv
/usr/lib/box1-erp/venv/bin/pip install --no-index --no-deps /tmp/box1-erp/box1_ops_erp_simulator-*.whl
ln -s /usr/lib/box1-erp/venv/bin/box1-erp /usr/bin/box1-erp
install -m0644 /tmp/box1-erp/systemd/box1-erp-worker.service /tmp/box1-erp/systemd/box1-erp-tui@.service /usr/lib/systemd/system/
install -D -m0755 /tmp/box1-erp/packaging/provision-data-volume.sh /usr/libexec/box1-erp/provision-data-volume.sh
install -D -m0640 -g box1erp /tmp/box1-erp/config/erp.conf.example /etc/box1-erp/erp.conf
install -d -m0755 /srv/box1-erp                 # mount point
```

The container harness (`harness/Containerfile`) performs these steps on CentOS Stream 10 and passes; it has not been done on RHEL 10 Image Mode.

## Storage

One data volume, mounted at **`/srv/box1-erp`** (root:root 0755), holding:

| Path | Owner, mode | Contents |
|---|---|---|
| `/srv/box1-erp/state/` | box1erp 0750 | `ledger.db` (+ `-wal`, `-shm`, `ledger.lock`), `spool/archive`, `spool/quarantine`, `backups/` |
| `/srv/box1-erp/exchange/` | root 0755 | SFTP chroot root (sshd requires root-owned, not group-writable) |
| `/srv/box1-erp/exchange/{inbound,outbound,consumed}` | root:erpxfer 2770 | the ERP1 areas |

One filesystem means every file move is a rename. `provision-data-volume.sh` creates this layout on the mounted volume, refuses if `/srv/box1-erp` is not a mount point, and never deletes data. Run it with `--init-ledger` **once**, at first provisioning; on later boots or rebuilds run it without the flag, or not at all. There is deliberately no tmpfiles entry: an unmounted volume must not get an empty layout on the root filesystem.

Proposed sizing, from the harness (worker memory peak 10.6 MB, ledger with a few requests under 1 MB): **1 vCPU, 1 GiB RAM, 2 GiB data disk**. Measure in the VM and adjust; backups kept on the volume need space too.

## systemd

#25 owns `box1-erp.target` (`Wants=box1-erp-worker.service box1-erp-tui@ttyS0.service`, `systemctl set-default box1-erp.target`), masking `serial-getty@ttyS0` (the TUI unit also `Conflicts=` it), and a drop-in `Environment=TERM=linux` for the `tty1` profile.

- `box1-erp-worker.service` is `Type=notify` with `WatchdogSec=60`; it exits 2 (no restart) when the volume, ledger or schema is wrong.
- Both units use `RequiresMountsFor=/srv/box1-erp`. **sshd should too**, so the adapter can't upload onto the root filesystem before the volume mounts: `/etc/systemd/system/sshd.service.d/box1-erp.conf` with `[Unit]` `RequiresMountsFor=/srv/box1-erp`. This also makes admin SSH depend on the volume; if #25 prefers admin SSH independent of it, run SFTP on a second sshd instance instead.
- `systemd-analyze security` (offline): worker **1.1 OK**, console **1.3 OK**.

## SSH and SFTP

`packaging/sshd-box1-erp-sftp.conf.example` is the reference `Match User erpsftp` block: chroot to `/srv/box1-erp/exchange`, `internal-sftp -u 0007`, publickey only, no TTY or forwarding, keys in `/etc/ssh/authorized_keys/%u` (root-owned 0644). The harness proves it with OpenSSH on CentOS Stream 10, including that the SFTP account cannot read the ledger, escape the chroot or get a shell, and that links, unreadable files and planted names in the exchange cannot damage the ledger.

## #25 must show (to close #14's VM criteria)

| #14 criterion | Evidence #25 provides |
|---|---|
| The VM boots directly into the operational TUI | Cold boot; serial console shows the ledger screen with no login; `systemctl get-default` = `box1-erp.target` |
| Reboots preserve ledger, audit history and processing mode | Set AUTO, create and ship a request, reboot; same records, audit rows and mode afterwards |
| TUI disconnect/restart does not interrupt background processing (VM half) | Disconnect `virtctl console` and `systemctl kill box1-erp-tui@ttyS0`; a file dropped meanwhile is imported |
| Worker failure is visible without taking down the TUI (VM half) | `systemctl stop box1-erp-worker`; console shows `WORKER STOPPED` and can still hold a request |
| Backup and restore with application-consistent data (VM half) | `box1-erp backup` while running, copy off the VM, restore into an isolated target (with both services stopped), compare records |
| Hardening the harness could not exercise | Both units start under SELinux enforcing **with** `PrivateDevices`, `ProtectKernel*`, `ProtectClock`, `ProtectHostname`, `Restrict*`, `LockPersonality`, `SystemCallArchitectures`, `SystemCallFilter`, `ProtectProc`/`ProcSubset` (rootless Podman forced `harness/worker-harness-only.conf`); `TTYPath=/dev/ttyS0` opens for the unprivileged `box1erp` |
| Rebuild keeps data | Rebuild the guest disk, keep the volume; the worker starts and the ledger is intact |

## #13 must show

| #14 criterion | Evidence #13 provides |
|---|---|
| A request submitted through #13 appears in the ledger | REST or Kafka request → file in `inbound/` → request in the console |
| Manual completion generates a result consumed by the adapter | Ship in the console → adapter status shows shipped, and `shipment.confirmed.v1` carries the same `shipmentId` |
| Protocol fixtures pass in the Camel adapter | The adapter's test suite run against the pinned box1-ops-contracts fixtures |
