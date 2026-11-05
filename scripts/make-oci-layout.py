#!/usr/bin/env python3
"""Package a release bundle directory as a one-layer OCI image layout (D-002, D-011).

Standard library only and deterministic: fixed timestamps, owners and file order, so
the same bundle gives the same digest. The image is FROM scratch: it is not meant to
run, only to carry the wheel, units and docs to #25's bootc build by digest:

    COPY --from=<registry>/box1-ci/box1-ops-erp-simulator@sha256:<digest> / /tmp/box1-erp/

Usage: make-oci-layout.py <bundle-dir> <layout-dir> <revision>
Then:  skopeo copy oci:<layout-dir>:latest docker://<repository>:<tag>
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import sys
import tarfile
from pathlib import Path

EPOCH = 315532800  # 1980-01-01, as for reproducible wheels


def layer_tar(bundle: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted(p for p in bundle.rglob("*")):
            rel = path.relative_to(bundle).as_posix()
            info = tarfile.TarInfo(rel)
            info.mtime, info.uid, info.gid, info.uname, info.gname = EPOCH, 0, 0, "", ""
            if path.is_symlink():
                raise SystemExit(f"refusing symlink in bundle: {rel}")
            if path.is_dir():
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(info)
            else:
                data = path.read_bytes()
                info.size, info.mode = len(data), 0o755 if os.access(path, os.X_OK) else 0o644
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def blob(layout: Path, data: bytes) -> tuple[str, int]:
    digest = hashlib.sha256(data).hexdigest()
    (layout / "blobs" / "sha256").mkdir(parents=True, exist_ok=True)
    (layout / "blobs" / "sha256" / digest).write_bytes(data)
    return f"sha256:{digest}", len(data)


def main(bundle: str, layout: str, revision: str) -> None:
    bundle_p, layout_p = Path(bundle), Path(layout)
    raw = layer_tar(bundle_p)
    gz = gzip.compress(raw, compresslevel=9, mtime=0)
    layer_digest, layer_size = blob(layout_p, gz)
    labels = {"org.opencontainers.image.revision": revision,
              "org.opencontainers.image.source": "https://github.com/box1-org/box1-ops-erp-simulator",
              "org.opencontainers.image.title": "box1-ops-erp-simulator release bundle"}
    config = {"architecture": "amd64", "os": "linux", "config": {"Labels": labels},
              "rootfs": {"type": "layers", "diff_ids": [f"sha256:{hashlib.sha256(raw).hexdigest()}"]},
              "history": [{"created_by": "scripts/make-oci-layout.py"}]}
    config_digest, config_size = blob(layout_p, json.dumps(config, sort_keys=True, separators=(",", ":")).encode())
    manifest = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": config_digest, "size": config_size},
                "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", "digest": layer_digest, "size": layer_size}],
                "annotations": labels}
    manifest_digest, manifest_size = blob(layout_p, json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode())
    (layout_p / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
    index = {"schemaVersion": 2, "manifests": [{"mediaType": "application/vnd.oci.image.manifest.v1+json",
             "digest": manifest_digest, "size": manifest_size,
             "annotations": {"org.opencontainers.image.ref.name": "latest"}}]}
    (layout_p / "index.json").write_text(json.dumps(index, sort_keys=True, separators=(",", ":")))
    print(manifest_digest)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    main(*sys.argv[1:])
