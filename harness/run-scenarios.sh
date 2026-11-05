#!/bin/sh
# Container-harness scenarios: real systemd, OpenSSH SFTP (chrooted, key-only) and SQLite.
# Usage: harness/run-scenarios.sh <work-dir>   (work-dir holds a throwaway SSH key; never commit it)
# Containers and images are named run14-*. Removes only its own container at the end.
set -u
WORK=${1:?work directory}
NAME=run14-erp-harness
IMAGE=localhost/run14-erp-harness:dev
PORT=${PORT:-2222}
cd "$(dirname "$0")/.."
mkdir -p "$WORK"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "PASS  $*"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL  $*"; }
check() { desc=$1; shift; if "$@" >/dev/null 2>&1; then ok "$desc"; else bad "$desc"; fi; }
x()    { podman exec "$NAME" "$@"; }
asapp() { podman exec -u box1erp -e BOX1_ERP_CONFIG=/etc/box1-erp/erp.conf "$NAME" /usr/lib/box1-erp/venv/bin/python -c "$1"; }
SSHOPTS="-i $WORK/key -o IdentitiesOnly=yes -o UserKnownHostsFile=$WORK/known_hosts -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=5 -P $PORT"
cat > "$WORK/sftpb" <<EOS
#!/bin/sh
# sftp batch helper: each argument is one sftp command
b=\$(mktemp "$WORK/batch.XXXXXX"); printf '%s\\n' "\$@" > "\$b"
sftp $SSHOPTS -b "\$b" erpsftp@127.0.0.1; rc=\$?; rm -f "\$b"; exit \$rc
EOS
chmod +x "$WORK/sftpb"
sftp_batch() { "$WORK/sftpb" "$@"; }
wait_until() { n=0; while [ $n -lt "${2:-60}" ]; do if sh -c "$1" >/dev/null 2>&1; then return 0; fi; sleep 1; n=$((n+1)); done; return 1; }

echo "== clocks: host $(date -u +%H:%M:%S) podman-vm $(podman machine ssh date -u +%H:%M:%S 2>/dev/null | tail -1)"
podman rm -f "$NAME" >/dev/null 2>&1
podman build -q -t "$IMAGE" -f harness/Containerfile . >/dev/null || { echo "build failed"; exit 1; }
echo "== image $(podman image inspect "$IMAGE" --format '{{.Id}}' | cut -c1-12)"
podman run -d --name "$NAME" --systemd=always -p "127.0.0.1:$PORT:22" "$IMAGE" >/dev/null
wait_until "podman exec $NAME systemctl is-system-running 2>/dev/null | grep -qE 'running|degraded'" 60
echo "== systemd: $(x systemctl is-system-running) default target: $(x systemctl get-default)"

echo "== units (the worker runs with harness/worker-harness-only.conf: 12 seccomp-based directives off)"
check "systemd-analyze verify accepts both application units" x systemd-analyze verify /usr/lib/systemd/system/box1-erp-worker.service /usr/lib/systemd/system/box1-erp-tui@.service
x systemd-analyze security --no-pager box1-erp-worker.service 2>/dev/null | tail -1

echo "== missing ledger: worker fails safe at boot, no ledger created"
check "worker unit failed (exit 2, no restart)" sh -c "podman exec $NAME systemctl show -p Result,ExecMainStatus box1-erp-worker | grep -q 'ExecMainStatus=2'"
check "no ledger was created" sh -c "! podman exec $NAME test -e /var/lib/box1-erp/ledger.db"
x journalctl -u box1-erp-worker --no-pager -o cat 2>/dev/null | grep -m1 'refusing to create' | cut -c1-160

echo "== provision ledger and start"
x runuser -u box1erp -- env BOX1_ERP_CONFIG=/etc/box1-erp/erp.conf /usr/lib/box1-erp/venv/bin/box1-erp init
x systemctl reset-failed box1-erp-worker
x systemctl start box1-erp-worker
check "worker active" x systemctl is-active box1-erp-worker
check "worker runs as box1erp" sh -c "podman exec $NAME ps -o user= -C box1-erp | grep -q box1erp"

echo "== SFTP account: key only, chrooted, no shell, no database"
rm -f "$WORK/key" "$WORK/key.pub" "$WORK/known_hosts"
ssh-keygen -q -t ed25519 -N '' -C run14-harness-only -f "$WORK/key"
podman cp "$WORK/key.pub" "$NAME:/etc/ssh/authorized_keys/erpsftp"
x chmod 0644 /etc/ssh/authorized_keys/erpsftp
SSHC="ssh -i $WORK/key -o IdentitiesOnly=yes -o UserKnownHostsFile=$WORK/known_hosts -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=5 -p $PORT"
wait_until "$WORK/sftpb 'ls' " 20
check "sftp login with the key works" $WORK/sftpb "ls"
out=$(sftp_batch "ls /" 2>&1); echo "   sftp ls / -> $(echo "$out" | tr '\n' ' ' | cut -c1-120)"
check "chroot root shows only the exchange areas" sh -c "echo \"$out\" | grep -q inbound && ! echo \"$out\" | grep -qE 'var|etc|usr'"
check "database path unreachable over SFTP" sh -c "! $WORK/sftpb 'ls /var/lib/box1-erp' 2>/dev/null"
check "cannot escape the chroot with .." sh -c "$WORK/sftpb 'cd ../..' 'pwd' 2>&1 | grep -q 'Remote working directory: /\$'"
shell=$($SSHC erpsftp@127.0.0.1 id 2>&1); echo "   ssh erpsftp id -> $(echo "$shell" | head -1)"
check "no shell or command for the SFTP account" sh -c "! echo \"$shell\" | grep -q 'uid='"
check "password authentication refused" sh -c "! ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password -o BatchMode=yes -o UserKnownHostsFile=$WORK/known_hosts -o ConnectTimeout=5 -p $PORT erpsftp@127.0.0.1 true 2>/dev/null"
check "erpsftp cannot read the ledger even outside the chroot" sh -c "! podman exec -u erpsftp $NAME cat /var/lib/box1-erp/ledger.db"
check "erpsftp is not in the box1erp group" sh -c "! podman exec $NAME id -nG erpsftp | grep -qw box1erp"

echo "== upload like the adapter: temp name, rename, then a result comes back"
printf 'HDR|ERP1|REQ|2026-11-05T22:00:00.000Z\nREQ|ful_h1|ord_h1|res_h1|site_lab|API|2026-11-05T22:00:00.000Z|corr-h1|\nLIN|1|PUMP-1|1\nTRL|4\n' > "$WORK/req1"
check "upload under a temp name and rename" $WORK/sftpb "put $WORK/req1 inbound/.REQ.ful_h1.0a1b2c3d.erp1.tmp" "rename inbound/.REQ.ful_h1.0a1b2c3d.erp1.tmp inbound/REQ.ful_h1.0a1b2c3d.erp1"
check "result revision 1 PENDING appears in outbound" wait_until "$WORK/sftpb 'ls outbound' 2>/dev/null | grep -q 'RES.ful_h1.000001'" 20
check "MANUAL: request imported, not shipped" sh -c "podman exec $NAME sqlite3 /var/lib/box1-erp/ledger.db \"SELECT state FROM requests WHERE request_id='ful_h1'\" | grep -qx PENDING"
f=$(sftp_batch 'ls -1 outbound' 2>/dev/null | grep RES.ful_h1.000001 | tr -d ' ' | sed 's#outbound/##')
check "adapter can move a result into consumed/ with a nonce" $WORK/sftpb "rename outbound/$f consumed/$f.1a2b3c4d"
check "the inbound file left the exchange (archived)" sh -c "! $WORK/sftpb 'ls inbound' 2>/dev/null | grep -q REQ.ful_h1"
x ls -l /srv/box1-erp/exchange | sed 's/^/   /'

echo "== crash after commit, restart by systemd, original shipment recovered"
x sed -i 's/^faults = false/faults = true/' /etc/box1-erp/erp.conf
x systemctl restart box1-erp-worker
asapp "
import os
from box1_erp import config, db, domain
c=db.connect(str(config.load().ledger))
domain.set_fault(c,'crash_after_commit','1',demo_enabled=True,actor='console',label='harness')
print('shipped', domain.ship(c,'ful_h1',expected_version=None,actor='console',label='harness'))"
check "worker crashed with exit 70" wait_until "podman exec $NAME journalctl -u box1-erp-worker --no-pager -o cat | grep -q 'one-shot crash'" 20
check "systemd restarted it (NRestarts>=1) and it is active" wait_until "podman exec $NAME systemctl show -p NRestarts box1-erp-worker | grep -qv 'NRestarts=0' && podman exec $NAME systemctl is-active box1-erp-worker" 30
check "SHIPPED result published after restart" wait_until "$WORK/sftpb 'ls outbound' 2>/dev/null | grep -q 'RES.ful_h1.000002'" 30
check "exactly one shipment" sh -c "podman exec $NAME sqlite3 /var/lib/box1-erp/ledger.db 'SELECT COUNT(*) FROM shipments' | grep -qx 1"
check "crash fault disarmed (no loop)" sh -c "podman exec $NAME sqlite3 /var/lib/box1-erp/ledger.db \"SELECT value FROM settings WHERE key='crash_after_commit'\" | grep -qx 0"
x systemctl show -p NRestarts,ActiveState,ExecMainStatus box1-erp-worker | tr '\n' ' '; echo

echo "== worker killed: systemd restarts it; ledger intact"
pid=$(x systemctl show -p MainPID --value box1-erp-worker)
x kill -9 "$pid"
check "worker back after SIGKILL" wait_until "podman exec $NAME systemctl is-active box1-erp-worker && [ \$(podman exec $NAME systemctl show -p MainPID --value box1-erp-worker) != $pid ]" 30
check "ledger integrity ok" sh -c "podman exec $NAME sqlite3 /var/lib/box1-erp/ledger.db 'PRAGMA integrity_check' | grep -qx ok"

echo "== backup while running, restore with the worker stopped"
b=$(x runuser -u box1erp -- env BOX1_ERP_CONFIG=/etc/box1-erp/erp.conf /usr/lib/box1-erp/venv/bin/box1-erp backup --label harness | tail -1)
echo "   backup at $b"
before=$(x sqlite3 /var/lib/box1-erp/ledger.db "SELECT request_id||state||COALESCE(shipment_id,'')||version FROM requests ORDER BY 1")
check "restore refused while the worker runs" sh -c "! podman exec -u box1erp -e BOX1_ERP_CONFIG=/etc/box1-erp/erp.conf $NAME /usr/lib/box1-erp/venv/bin/box1-erp restore $b"
x systemctl stop box1-erp-worker
x rm -f /var/lib/box1-erp/ledger.db /var/lib/box1-erp/ledger.db-wal /var/lib/box1-erp/ledger.db-shm
check "restore succeeds with the worker stopped" x runuser -u box1erp -- env BOX1_ERP_CONFIG=/etc/box1-erp/erp.conf /usr/lib/box1-erp/venv/bin/box1-erp restore "$b"
after=$(x sqlite3 /var/lib/box1-erp/ledger.db "SELECT request_id||state||COALESCE(shipment_id,'')||version FROM requests ORDER BY 1")
check "restored records match (request, state, shipment, version)" [ "$before" = "$after" ]
x systemctl start box1-erp-worker
check "worker active after restore" x systemctl is-active box1-erp-worker
echo "   records: $after"

echo "== journald, not the console"
x journalctl -u box1-erp-worker --no-pager -o cat -n 3 | cut -c1-160 | sed 's/^/   /'

echo "== result: $PASS passed, $FAIL failed"
podman rm -f "$NAME" >/dev/null 2>&1
[ "$FAIL" -eq 0 ]
