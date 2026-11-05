"""Shared test setup: a real appliance layout in a temp directory."""
import os
import subprocess
import sys
import time
from pathlib import Path

from box1_erp import config, db, protocol

SRC = str(Path(__file__).resolve().parents[1] / "src")


def make_appliance(tmp: Path, demo: bool = False, poll: float = 0.2) -> config.Config:
    state, exch = tmp / "state", tmp / "exchange"
    for d in (state / "spool" / "archive", state / "spool" / "quarantine", state / "backups",
              exch / "inbound", exch / "outbound", exch / "consumed"):
        d.mkdir(parents=True, exist_ok=True)
    conf = tmp / "erp.conf"
    conf.write_text(f"[paths]\nstate_dir = {state}\nexchange_dir = {exch}\n[demo]\nfaults = {'true' if demo else 'false'}\n"
                    f"[worker]\npoll_interval = {poll}\nheartbeat_stale_seconds = 3\n")
    cfg = config.load(str(conf))
    db.initialize(str(cfg.ledger))
    return cfg


def request_bytes(ful="ful_1", order="ord_1", qty=2, sku="SKU-1", lines=None):
    rows = [["HDR", "ERP1", "REQ", "2026-11-05T22:00:00.000Z"],
            ["REQ", ful, order, "res_1", "site_a", "EVENT", "2026-11-05T21:59:00.000Z", "corr-" + ful, ""]]
    lines = lines or [(sku, qty)]
    rows += [["LIN", str(i), s, str(q)] for i, (s, q) in enumerate(lines, 1)]
    rows.append(["TRL", str(len(rows) + 1)])
    return protocol.render(rows)


def drop(cfg, name: str, data: bytes) -> None:
    """Upload like the adapter: temp name, then rename."""
    tmp = cfg.inbound / f".{name}.tmp"
    tmp.write_bytes(data)
    os.replace(tmp, cfg.inbound / name)


def env_for(cfg_path: str) -> dict:
    env = dict(os.environ, PYTHONPATH=SRC, BOX1_ERP_CONFIG=cfg_path)
    return env


def start_worker(cfg_path: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "box1_erp.cli", "worker"], env=env_for(cfg_path),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def wait_for(predicate, timeout=15.0, interval=0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout:.0f}s")


def q(cfg, sql, *args):
    conn = db.connect(str(cfg.ledger))
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()
