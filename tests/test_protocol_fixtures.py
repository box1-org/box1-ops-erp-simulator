"""ERP1 golden fixtures from box1-ops-contracts, checked by the ERP's own reader and writer."""
import json
from datetime import UTC

import pytest
from conftest import contracts_dir, fixtures_dir

from box1_erp import protocol

MANIFEST = json.loads((fixtures_dir() / "manifest.json").read_text()) if (fixtures_dir() / "manifest.json").exists() else {"files": []}


@pytest.mark.parametrize("fx", MANIFEST["files"], ids=lambda f: f["file"])
def test_fixture(fx, fixture_bytes):
    data = fixture_bytes(fx["file"])
    if fx["valid"]:
        parsed = protocol.parse(data, fx["name"])
        assert parsed.file_type == fx["fileType"]
        assert parsed.records == fx["records"]
    else:
        assert fx["errorCode"] in protocol.ERROR_CODES
        with pytest.raises(protocol.ProtocolError) as err:
            protocol.parse(data, fx["name"])
        assert err.value.code == fx["errorCode"], err.value.detail


def test_manifest_is_present_and_complete(manifest):
    assert manifest["protocol"] == "ERP1"
    assert len(manifest["files"]) >= 40
    covered = {f["errorCode"] for f in manifest["files"] if not f["valid"]}
    assert set(protocol.ERROR_CODES) - {"CONFLICTING_REUSE", "UNREADABLE"} <= covered  # neither can be a file fixture


def test_equivalence(manifest, fixture_bytes):
    names = {f["file"]: f["name"] for f in manifest["files"]}
    for pair in manifest["equivalence"]:
        a = protocol.parse(fixture_bytes(pair["a"]), names[pair["a"]])
        b = protocol.parse(fixture_bytes(pair["b"]), names[pair["b"]])
        assert (protocol.equivalence_key(a) == protocol.equivalence_key(b)) is pair["equivalent"], pair["why"]


def test_name_classification(manifest):
    for n in manifest["names"]:
        assert protocol.classify(n["area"], n["name"]) == n["expect"], n


def test_writer_reproduces_valid_result_fixtures_byte_for_byte(manifest, fixture_bytes):
    for fx in manifest["files"]:
        if not fx["valid"] or fx["fileType"] == "REQ":
            continue
        rows = [[r["type"], *[v for k, v in r.items() if k != "type"]] for r in fx["records"]]
        assert protocol.render(rows) == fixture_bytes(fx["file"]), fx["file"]


def test_mapping_from_fulfillment_requested_event(manifest, fixture_bytes):
    events = json.loads((contracts_dir() / "fixtures" / "events.json").read_text())
    for m in manifest["mapping"]:
        event = next(e for e in events if e["eventId"] == m["eventId"])
        p = event["payload"]
        # Python's own mapping per SPEC section 8; #13 implements the same in the adapter.
        from datetime import datetime
        at = datetime.fromisoformat(event["occurredAt"].replace("Z", "+00:00")).astimezone(UTC)
        requested_at = at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"
        produced = protocol.render([
            ["HDR", "ERP1", "REQ", m["createdAt"]],
            ["REQ", p["fulfillmentRequestId"], p["orderId"], p["reservationId"], p["siteId"], "EVENT", requested_at,
             event.get("correlationId", ""), event.get("traceparent", "")],
            ["LIN", "1", p["sku"], str(p["quantity"])],
            ["TRL", "4"],
        ])
        assert produced == fixture_bytes(m["expected"])


def test_code_lists_match_the_specification():
    """The reader's code lists must equal the ones SPEC.md names (UNREADABLE once drifted)."""
    import re
    spec = (fixtures_dir().parent / "SPEC.md").read_text()
    para = next(line for line in spec.splitlines() if line.startswith("**Error codes**"))
    assert set(re.findall(r"`([A-Z_]+)`", para)) - {"ERR"} == set(protocol.ERROR_CODES)
    para = next(line for line in spec.splitlines() if line.startswith("**Rejection reason codes.**"))
    assert set(re.findall(r"`([A-Z_]+)`", para)) == set(protocol.REJECTION_CODES)
