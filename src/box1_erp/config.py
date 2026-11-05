"""Configuration from /etc/box1-erp/erp.conf (or BOX1_ERP_CONFIG). Root-owned; the app only reads it."""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PATH = "/etc/box1-erp/erp.conf"


@dataclass(frozen=True)
class Config:
    state_dir: Path          # data volume: ledger.db, spool/, backups/
    exchange_dir: Path       # SFTP exchange: inbound/, outbound/, consumed/
    demo_faults: bool = False
    poll_interval: float = 1.0
    heartbeat_stale_seconds: float = 10.0
    consumed_retention_days: int = 7

    @property
    def ledger(self) -> Path:
        return self.state_dir / "ledger.db"

    @property
    def archive(self) -> Path:
        return self.state_dir / "spool" / "archive"

    @property
    def quarantine(self) -> Path:
        return self.state_dir / "spool" / "quarantine"

    @property
    def backups(self) -> Path:
        return self.state_dir / "backups"

    @property
    def inbound(self) -> Path:
        return self.exchange_dir / "inbound"

    @property
    def outbound(self) -> Path:
        return self.exchange_dir / "outbound"

    @property
    def consumed(self) -> Path:
        return self.exchange_dir / "consumed"


def load(path: str | None = None) -> Config:
    path = path or os.environ.get("BOX1_ERP_CONFIG", DEFAULT_PATH)
    cp = configparser.ConfigParser()
    if not cp.read(path):
        raise FileNotFoundError(f"configuration not found: {path}")
    paths, demo, worker = cp["paths"], cp["demo"] if cp.has_section("demo") else {}, cp["worker"] if cp.has_section("worker") else {}
    return Config(
        state_dir=Path(paths["state_dir"]),
        exchange_dir=Path(paths["exchange_dir"]),
        demo_faults=str(demo.get("faults", "false")).strip().lower() in ("1", "true", "yes", "on"),
        poll_interval=float(worker.get("poll_interval", 1.0)),
        heartbeat_stale_seconds=float(worker.get("heartbeat_stale_seconds", 10.0)),
        consumed_retention_days=int(worker.get("consumed_retention_days", 7)),
    )
