# box1 legacy ERP appliance

A deliberately legacy ERP for the box1 modernization demonstration ([box1-docs#14](https://github.com/box1-org/box1-docs/issues/14)). It runs natively in a Linux VM ([#25](https://github.com/box1-org/box1-docs/issues/25)), boots into a curses console, keeps its ledger in SQLite, and talks to the modern world only through pipe-delimited files over SFTP, exchanged with the fulfillment adapter ([#13](https://github.com/box1-org/box1-docs/issues/13)).

There is **no HTTP API, no OpenAPI document and no Kafka integration**, by design.

```text
Adapter --SFTP--> inbound/  --> worker --> SQLite ledger <-- console TUI
Adapter <--SFTP-- outbound/ <-- worker (results, error files)
```

## What is in here

| Path | What |
|---|---|
| `src/box1_erp/protocol.py` | ERP1 reader and writer. The protocol and its golden fixtures live in [box1-ops-contracts `legacy/erp-file/v1/`](https://github.com/box1-org/box1-ops-contracts/tree/main/legacy/erp-file/v1); `contracts.lock` pins the commit tests use |
| `src/box1_erp/domain.py` | Every business rule: identity, equivalence, transitions, shipments, audit, result intents. Worker and console both call it |
| `src/box1_erp/worker.py` | `box1-erp-worker.service`: ingestion, AUTO processing, publication, quarantine, replay, recovery |
| `src/box1_erp/tui.py` | `box1-erp-tui@.service`: the operator console |
| `src/box1_erp/backup.py` | Application-consistent backup and restore |
| `systemd/` | The two application units |
| `packaging/` | sysusers (fixed IDs), the data-volume provisioning script and a reference sshd snippet for #25 |
| `config/erp.conf.example` | `/etc/box1-erp/erp.conf` |
| `harness/` | Container harness (CentOS Stream 10, systemd, OpenSSH, a data volume). Not a substitute for the VM |
| `.tekton/` | Pipelines as Code PR and release runs, inert until the repository is registered (D-002) |
| `docs/RUNBOOK.md` | Operating and recovering the appliance |
| `docs/VM-INTEGRATION.md` | What #25 installs, and what it must prove in the VM |
| `docs/DESIGN.md` | States, transitions, ledger, durability and fault design |

Runtime needs only Python 3.12's standard library (RHEL 10's system Python). Nothing is downloaded at boot.

## Develop and test

```sh
uv venv -p 3.12 .venv && uv pip install -p .venv -r requirements-dev.txt
scripts/fetch-contracts.sh                 # pinned golden fixtures into .contracts/
.venv/bin/ruff check src tests
.venv/bin/pytest -q                        # unit, fixture, worker, console (pty) and backup tests
harness/run-scenarios.sh /tmp/erp-harness  # needs podman; builds dist/ first: python -m build
```

Run locally against a scratch layout with `BOX1_ERP_CONFIG=<file> python -m box1_erp.cli init|worker|tui|status|backup|restore`.

## Ownership

#14 (this repository): the application, protocol, fixtures, application units and their documentation. #25: the VM, image, `box1-erp.target`, console selection, mounts, sshd, SELinux and backup storage. #13: the Camel adapter.
