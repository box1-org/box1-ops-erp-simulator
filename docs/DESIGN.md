# Design

Decision records with alternatives are in box1-docs `agent-runs/14-erp-appliance/decisions/` (D-001 to D-014).

## Business states

| From | Action | To | Who |
|---|---|---|---|
| (new file) | import | `PENDING` | worker |
| `PENDING` | hold | `HELD` | console |
| `HELD` | release | `PENDING` | console |
| `PENDING`, `HELD` | ship | `SHIPPED` (terminal) | console; worker only from `PENDING` in AUTO |
| `PENDING`, `HELD` | reject (code + reason) | `REJECTED` (terminal) | console |

Re-emit and mode changes do not change a request's state. Each state change writes one result revision (ERP1 section 7). Publication state (`PENDING_PUBLISH`, `PUBLISHED`) is separate from business state and lives on the result.

## Ledger

SQLite in WAL mode, `synchronous=FULL`, `busy_timeout=5000`, foreign keys on. Every mutation is one `BEGIN IMMEDIATE` transaction in `domain.py`. The console reads, waits for the operator, then calls one domain function carrying the version it showed; a changed request is refused and redisplayed. `UNIQUE(request_id)` on shipments is the database backstop for "one shipment per request". Ship commits shipment, transition, audit row and result intent together.

## Files

Inbound files are parsed, committed, then moved to the private archive or quarantine. Results are written as `.<name>.tmp`, `fsync`ed, renamed, and the directory `fsync`ed before the ledger marks them published (D-009). Restart republishes anything not marked; stale temp files are removed.

## Demo faults

Enabled only by `faults = true` in the root-owned config. Pause publication, delay AUTO, and arm a one-shot crash after the next shipment commit. The crash disarms in its own committed transaction before `os._exit(70)`, so a restart cannot loop. All changes are audited and shown on the console status line.

## Operator identity

The operator label is an audit label typed at the console, not an authenticated identity. The console is a shared session protected by VM-console access (#25 RBAC).
