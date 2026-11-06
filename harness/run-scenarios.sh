#!/bin/sh
# Container-harness scenarios: real systemd, OpenSSH SFTP (chrooted, key-only) and SQLite.
# Usage: harness/run-scenarios.sh <work-dir>   (work-dir holds a throwaway SSH key; never commit it)
# Build dist/ first (python -m build). Container, volume and image are named run14-*; only those are removed.
set -u
WORK=${1:?work directory}
NAME=run14-erp-harness
VOL=run14-erp-data
IMAGE=localhost/run14-erp-harness:dev
PORT=${PORT:-2222}
cd "$(dirname "$0")/.."
mkdir -p "$WORK"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "PASS  $*"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL  $*"; }
check() { desc=$1; shift; if "$@" >/dev/null 2>&1; then ok "$desc"; else bad "$desc"; fi; }
x()    { podman exec "$NAME" "$@"; }
erp()  { podman exec -u box1erp -e BOX1_ERP_CONFIG=/etc/box1-erp/erp.conf "$NAME" /usr/bin/box1-erp "$@"; }
asapp() { podman exec -u box1erp -e BOX1_ERP_CONFIG=/etc/box1-erp/erp.conf "$NAME" /usr/lib/box1-erp/venv/bin/python -c "$1"; }
sql()  { x sqlite3 /srv/box1-erp/state/ledger.db "$1"; }
SSHOPTS="-i $WORK/key -o IdentitiesOnly=yes -o UserKnownHostsFile=$WORK/known_hosts -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=5 -P $PORT"
cat > "$WORK/sftpb" <<EOS
#!/bin/sh
# sftp batch helper: each argument is one sftp command
b=\$(mktemp "$WORK/batch.XXXXXX"); printf '%s\\n' "\$@" > "\$b"
sftp $SSHOPTS -b "\$b" erpsftp@127.0.0.1; rc=\$?; rm -f "\$b"; exit \$rc
EOS
chmod +x "$WORK/sftpb"
SB="$WORK/sftpb"
wait_until() { n=0; while [ $n -lt "${2:-60}" ]; do if sh -c "$1" >/dev/null 2>&1; then return 0; fi; sleep 1; n=$((n+1)); done; return 1; }
req() { printf 'HDR|ERP1|REQ|2026-11-05T22:00:00.000Z\nREQ|%s|ord_%s|res_h|site_lab|API|2026-11-05T22:00:00.000Z|corr-%s|\nLIN|1|PUMP-1|1\nTRL|4\n' "$1" "$1" "$1"; }
upload() { req "$1" > "$WORK/$1"; "$SB" "put $WORK/$1 inbound/.REQ.$1.$2.erp1.tmp" "rename inbound/.REQ.$1.$2.erp1.tmp inbound/REQ.$1.$2.erp1"; }
LEDGER=/srv/box1-erp/state/ledger.db

echo "== clocks: host $(date -u +%H:%M:%S) podman-vm $(podman machine ssh date -u +%H:%M:%S 2>/dev/null | tail -1)"
podman rm -f "$NAME" >/dev/null 2>&1; podman volume rm -f "$VOL" >/dev/null 2>&1
podman build -q -t "$IMAGE" -f harness/Containerfile . >/dev/null || { echo "build failed"; exit 1; }
echo "== image $(podman image inspect "$IMAGE" --format '{{.Id}}' | cut -c1-12); wheel sha256 $(shasum -a 256 dist/*.whl | cut -c1-16)"
podman volume create "$VOL" >/dev/null
podman run -d --name "$NAME" --systemd=always -p "127.0.0.1:$PORT:22" --mount "type=volume,src=$VOL,dst=/srv/box1-erp" "$IMAGE" >/dev/null
wait_until "podman exec $NAME systemctl is-system-running 2>/dev/null | grep -qE 'running|degraded'" 60
echo "== $(x systemctl --version | head -1 | cut -d' ' -f1-2): $(x systemctl is-system-running), default target $(x systemctl get-default)"

echo "== units (worker runs with harness/worker-harness-only.conf: seccomp-based directives off, WatchdogSec=10)"
check "systemd-analyze verify accepts both shipped units" x systemd-analyze verify /usr/lib/systemd/system/box1-erp-worker.service /usr/lib/systemd/system/box1-erp-tui@.service
x systemd-analyze security --no-pager box1-erp-worker.service 2>/dev/null | tail -1
check "fixed IDs: box1erp 962, erpsftp 963, erpxfer 961" sh -c "podman exec $NAME id box1erp | grep -q 'uid=962' && podman exec $NAME id erpsftp | grep -q 'uid=963' && podman exec $NAME getent group erpxfer | grep -q ':961:'"

echo "== empty data volume: worker fails safe, nothing created"
check "worker failed with exit 2 and is not restarting" sh -c "podman exec $NAME systemctl show -p ExecMainStatus,ActiveState box1-erp-worker | tr '\n' ' ' | grep -q 'ExecMainStatus=2 ActiveState=failed'"
check "no ledger and no state directory were created" sh -c "! podman exec $NAME test -e /srv/box1-erp/state"
x /usr/libexec/box1-erp/provision-data-volume.sh
x systemctl reset-failed box1-erp-worker; x systemctl start box1-erp-worker 2>/dev/null; sleep 2
check "provisioned without a ledger: still exit 2" sh -c "podman exec $NAME systemctl show -p ExecMainStatus box1-erp-worker | grep -q 'ExecMainStatus=2'"
check "still no ledger" sh -c "! podman exec $NAME test -e $LEDGER"

echo "== first provisioning creates the ledger; worker starts (Type=notify)"
x /usr/libexec/box1-erp/provision-data-volume.sh --init-ledger
x systemctl reset-failed box1-erp-worker; x systemctl start box1-erp-worker
check "worker active (READY=1 received)" x systemctl is-active box1-erp-worker
check "worker runs as box1erp" sh -c "podman exec $NAME ps -o user= -C box1-erp | grep -q box1erp"
x ls -ln /srv/box1-erp /srv/box1-erp/exchange | sed 's/^/   /'

echo "== SFTP account: key only, chrooted, no shell, no database"
rm -f "$WORK/key" "$WORK/key.pub" "$WORK/known_hosts"
ssh-keygen -q -t ed25519 -N '' -C run14-harness-only -f "$WORK/key"
podman cp "$WORK/key.pub" "$NAME:/etc/ssh/authorized_keys/erpsftp"
x chmod 0644 /etc/ssh/authorized_keys/erpsftp
wait_until "$SB ls" 20
check "sftp login with the key works" "$SB" "ls"
out=$("$SB" "ls /" 2>&1); echo "   sftp ls / -> $(echo "$out" | tr '\n' ' ' | cut -c1-120)"
check "chroot root shows only the exchange areas" sh -c "echo \"$out\" | grep -q inbound && ! echo \"$out\" | grep -qE 'state|var|etc|usr'"
check "state directory unreachable over SFTP" sh -c "! $SB 'ls /srv/box1-erp/state' 2>/dev/null"
check "cannot escape the chroot with .." sh -c "$SB 'cd ../..' 'pwd' 2>&1 | grep -q 'Remote working directory: /\$'"
shell=$(ssh -i "$WORK/key" -o IdentitiesOnly=yes -o UserKnownHostsFile="$WORK/known_hosts" -o BatchMode=yes -o ConnectTimeout=5 -p "$PORT" erpsftp@127.0.0.1 id 2>&1)
echo "   ssh erpsftp id -> $(echo "$shell" | head -1)"
check "no shell or command for the SFTP account" sh -c "! echo \"$shell\" | grep -q 'uid='"
check "password authentication refused" sh -c "! ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password -o BatchMode=yes -o UserKnownHostsFile=$WORK/known_hosts -o ConnectTimeout=5 -p $PORT erpsftp@127.0.0.1 true 2>/dev/null"
check "erpsftp cannot read the ledger even outside the chroot" sh -c "! podman exec -u erpsftp $NAME cat $LEDGER"

echo "== adapter-style upload: temp name, rename, result back"
check "upload under a temp name and rename" upload ful_h1 0a1b2c3d
check "result revision 1 PENDING appears in outbound" wait_until "$SB 'ls outbound' 2>/dev/null | grep -q 'RES.ful_h1.000001'" 20
check "MANUAL: imported, not shipped" sh -c "podman exec $NAME sqlite3 $LEDGER \"SELECT state FROM requests WHERE request_id='ful_h1'\" | grep -qx PENDING"
f=$("$SB" 'ls -1 outbound' 2>/dev/null | grep RES.ful_h1.000001 | tr -d ' ' | sed 's#outbound/##')
check "adapter moves a result into consumed/ with a nonce" "$SB" "rename outbound/$f consumed/$f.1a2b3c4d"
check "the inbound file left the exchange" sh -c "! $SB 'ls inbound' 2>/dev/null | grep -q REQ.ful_h1"
check "archived copy is owned by box1erp, not erpsftp" sh -c "[ \$(podman exec $NAME find /srv/box1-erp/state/spool/archive -type f ! -user box1erp | wc -l) -eq 0 ]"

echo "== attacks by the SFTP account (review findings S1, M1, M7)"
check "plant a symlink to the ledger at the published result name" "$SB" "symlink /srv/box1-erp/state/ledger.db outbound/$f"
check "an equivalent repeat forces a republish of that name" upload ful_h1 0000ffff
wait_until "podman exec $NAME test -f /srv/box1-erp/exchange/outbound/$f -a ! -L /srv/box1-erp/exchange/outbound/$f" 20
check "ledger intact (integrity ok)" sh -c "podman exec $NAME sqlite3 $LEDGER 'PRAGMA integrity_check' | grep -qx ok"
check "result republished as a regular file over the link" x test -f "/srv/box1-erp/exchange/outbound/$f" -a ! -L "/srv/box1-erp/exchange/outbound/$f"
"$SB" "symlink /srv/box1-erp/state/ledger.db inbound/REQ.ful_lnk.0a1b2c3d.erp1" >/dev/null 2>&1
req ful_lock > "$WORK/ful_lock"
"$SB" "put $WORK/ful_lock inbound/.REQ.ful_lock.0a1b2c3d.erp1.tmp" "chmod 000 inbound/.REQ.ful_lock.0a1b2c3d.erp1.tmp" "rename inbound/.REQ.ful_lock.0a1b2c3d.erp1.tmp inbound/REQ.ful_lock.0a1b2c3d.erp1" >/dev/null 2>&1
upload ful_after 0a1b2c3d >/dev/null 2>&1
check "a request uploaded after them is still imported" wait_until "podman exec $NAME sqlite3 $LEDGER \"SELECT 1 FROM requests WHERE request_id='ful_after'\" | grep -qx 1" 20
check "symlink and mode-000 file quarantined unopened as UNREADABLE" sh -c "podman exec $NAME sqlite3 $LEDGER \"SELECT COUNT(*) FROM quarantine WHERE error_code='UNREADABLE'\" | grep -qx 2"
check "ERR files for both are in outbound" sh -c "[ \$($SB 'ls outbound' 2>/dev/null | grep -c 'ERR.err_') -ge 2 ]"
check "inbound is empty again" sh -c "[ -z \"\$(podman exec $NAME ls -A /srv/box1-erp/exchange/inbound)\" ]"
check "ledger still intact" sh -c "podman exec $NAME sqlite3 $LEDGER 'PRAGMA integrity_check' | grep -qx ok"

echo "== crash after commit: systemd restarts the worker, original shipment recovered"
x sed -i 's/^faults = false/faults = true/' /etc/box1-erp/erp.conf
x systemctl restart box1-erp-worker
asapp "
from box1_erp import config, db, domain
c=db.connect(str(config.load().ledger))
domain.set_fault(c,'crash_after_commit','1',demo_enabled=True,actor='console',label='harness')
print('   shipped', domain.ship(c,'ful_h1',expected_version=None,actor='console',label='harness'))"
check "worker crashed with exit 70" wait_until "podman exec $NAME journalctl -u box1-erp-worker --no-pager -o cat | grep -q 'one-shot crash'" 20
check "systemd restarted it and it is active" wait_until "podman exec $NAME systemctl show -p NRestarts box1-erp-worker | grep -qv 'NRestarts=0' && podman exec $NAME systemctl is-active box1-erp-worker" 30
check "SHIPPED result published after restart" wait_until "$SB 'ls outbound' 2>/dev/null | grep -q 'RES.ful_h1.000002'" 30
check "exactly one shipment" sh -c "[ \$(podman exec $NAME sqlite3 $LEDGER 'SELECT COUNT(*) FROM shipments') = 1 ]"
check "crash fault disarmed (no loop)" sh -c "podman exec $NAME sqlite3 $LEDGER \"SELECT value FROM settings WHERE key='crash_after_commit'\" | grep -qx 0"

echo "== hung worker: the watchdog kills and restarts it (review finding O5)"
r0=$(x systemctl show -p NRestarts --value box1-erp-worker)
pid=$(x systemctl show -p MainPID --value box1-erp-worker)
x kill -STOP "$pid"
check "watchdog restarted the frozen worker" wait_until "[ \$(podman exec $NAME systemctl show -p NRestarts --value box1-erp-worker) -gt $r0 ] && podman exec $NAME systemctl is-active box1-erp-worker" 45
x journalctl -u box1-erp-worker --no-pager -o cat | grep -m1 -i watchdog | sed 's/^/   /'

echo "== monitoring"
erp status --check >/dev/null 2>&1; rc=$?
check "status --check exits 1 (quarantine needs attention)" [ "$rc" -eq 1 ]
erp status | grep -E '"(worker|oldest_pending_age_s|oldest_quarantine_age_s|quarantined)"' | sed 's/^/   /'

echo "== backup while running; restore refused while worker or console holds the ledger; restore"
b=$(erp backup --label harness | tail -1); echo "   backup at $b"
before=$(sql "SELECT request_id||state||COALESCE(shipment_id,'')||version FROM requests ORDER BY 1")
erp restore "$b" >/dev/null 2>&1; rc=$?
check "restore refused while the worker runs" [ "$rc" -ne 0 ]
x systemctl stop box1-erp-worker
asapp "
import time
from box1_erp import config, db
l=db.LedgerLock(str(config.load().ledger)); assert l.acquire(); time.sleep(8)" &
sleep 3
erp restore "$b" >/dev/null 2>&1; rc=$?
check "restore refused while a console holds the ledger" [ "$rc" -ne 0 ]
wait
check "restore succeeds with worker and consoles stopped" erp restore "$b"
after=$(sql "SELECT request_id||state||COALESCE(shipment_id,'')||version FROM requests ORDER BY 1")
check "restored records match (request, state, shipment, version)" [ "$before" = "$after" ]
check "previous ledger kept aside" sh -c "podman exec $NAME sh -c 'ls /srv/box1-erp/state/ledger.pre-restore-*.db'"
check "restored ledger is in MANUAL" sh -c "[ \$(podman exec $NAME sqlite3 $LEDGER \"SELECT value FROM settings WHERE key='mode'\") = MANUAL ]"
x systemctl start box1-erp-worker
check "worker active after restore" x systemctl is-active box1-erp-worker
echo "   records: $after"

echo "== resources (worker, after the run)"
x systemctl show -p MemoryCurrent,MemoryPeak,CPUUsageNSec box1-erp-worker | tr '\n' ' ' | sed 's/^/   /'; echo
x du -sh /srv/box1-erp/state | sed 's/^/   data: /'

echo "== journald, not the console"
x journalctl -u box1-erp-worker --no-pager -o cat -n 3 | cut -c1-200 | sed 's/^/   /'

echo "== result: $PASS passed, $FAIL failed"
podman rm -f "$NAME" >/dev/null 2>&1; podman volume rm -f "$VOL" >/dev/null 2>&1
[ "$FAIL" -eq 0 ]
