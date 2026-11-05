# VM integration contract for #25

What this repository provides, how #25 installs it, and what #25 must show in the VM to close #14's VM criteria.

## Artifact

`box1_ops_erp_simulator-<version>-py3-none-any.whl` plus `systemd/`, `packaging/` and `config/`. Standard library only (Python 3.12). Proposed delivery (D-002, needs approval): an OCI image `FROM scratch` holding these files, pushed to `box1-ci/box1-ops-erp-simulator@sha256:<digest>`, consumed in the bootc Containerfile with `COPY --from=<image>@sha256:<digest>`. Pin by digest; the wheel's own sha256 is listed in `SHA256SUMS` inside the image.

## Install (image build time)

```sh
python3 -m venv --system-site-packages /usr/lib/box1-erp/venv
/usr/lib/box1-erp/venv/bin/pip install --no-index --no-deps box1_ops_erp_simulator-*.whl
install -m0644 systemd/box1-erp-worker.service systemd/box1-erp-tui@.service /usr/lib/systemd/system/
install -m0644 packaging/sysusers-box1-erp.conf /usr/lib/sysusers.d/box1-erp.conf
install -m0644 packaging/tmpfiles-box1-erp.conf /usr/lib/tmpfiles.d/box1-erp.conf
install -m0640 -g box1erp config/erp.conf.example /etc/box1-erp/erp.conf     # after sysusers
```

`packaging/sshd-box1-erp-sftp.conf.example` is a reference for #25's sshd configuration (chroot, key-only, no TTY or forwarding, keys in `/etc/ssh/authorized_keys/%u`). The container harness proves it works with OpenSSH on CentOS Stream 10.

## #25 owns

- `box1-erp.target` (`Wants=box1-erp-worker.service box1-erp-tui@ttyS0.service`), `systemctl set-default box1-erp.target`, masking the competing getty, and a `TERM=linux` drop-in for the `tty1` profile.
- The data volume mounted at `/var/lib/box1-erp` and `/srv/box1-erp` (one filesystem, so file moves are renames; the worker falls back to copy-fsync-unlink otherwise), and running `box1-erp init` once at first provisioning only.
- SELinux labels for the two trees, sshd, host keys, network policy and backup storage.

## What #25 must show to close #14's VM criteria

| #14 criterion | Evidence #25 provides |
|---|---|
| The VM boots directly into the operational TUI | Cold boot; serial console shows the ledger screen with no login; `systemctl get-default` = `box1-erp.target` |
| Reboots preserve ledger, audit history and processing mode | Set AUTO, create and ship a request, reboot; same records, audit rows and mode afterwards |
| TUI disconnect/restart does not interrupt background processing (VM half) | Disconnect `virtctl console` and `systemctl kill box1-erp-tui@ttyS0`; a file dropped meanwhile is imported |
| Worker failure is visible without taking down the TUI (VM half) | `systemctl stop box1-erp-worker`; console shows `WORKER STOPPED` and still holds a request |
| Backup and restore with application-consistent data (VM half) | `box1-erp backup` while running, copy off the VM, restore into an isolated target, compare records |
| Hardening that the container harness could not exercise | The shipped units start under SELinux enforcing **with** `PrivateDevices`, `ProtectKernel*`, `ProtectClock`, `ProtectHostname`, `Restrict*`, `LockPersonality`, `SystemCallArchitectures` (the harness had to turn these off; `harness/worker-harness-only.conf`). `TTYPath=/dev/ttyS0` opens as `box1erp` |

## What #13 must show

| #14 criterion | Evidence #13 provides |
|---|---|
| A request submitted through #13 appears in the ledger | REST or Kafka request → file in `inbound/` → the request in the console |
| Manual completion generates a result consumed by the adapter | Ship in the console → adapter status shows shipped, and `shipment.confirmed.v1` with the same `shipmentId` |
| Protocol fixtures pass in the Camel adapter | The adapter's test suite run against the pinned box1-ops-contracts fixtures |
