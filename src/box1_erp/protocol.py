"""ERP1 legacy file protocol: reader, writer, equivalence and file-name rules.

Implements box1-ops-contracts legacy/erp-file/v1/SPEC.md. The golden fixtures
live in that repository at the commit pinned in contracts.lock; tests fetch
them rather than copying them here.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field

MAGIC = "ERP1"
MAX_FILE_BYTES = 65536
MAX_LINES = 99

ERROR_CODES = (
    "UNREADABLE", "FILE_TOO_LARGE", "ENCODING", "TRUNCATED", "MALFORMED_RECORD", "UNSUPPORTED_VERSION", "RECORD_ORDER",
    "MISSING_FIELD", "FIELD_LENGTH", "FIELD_VALUE", "COUNT_MISMATCH", "FILENAME_MISMATCH", "CONFLICTING_REUSE",
)
STATUSES = ("PENDING", "HELD", "SHIPPED", "REJECTED")
REJECTION_CODES = ("STOCK_UNAVAILABLE", "SITE_UNAVAILABLE", "DAMAGED_GOODS", "OPERATOR_CANCELLED", "OTHER")

_TS = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{3})Z")
_TRACEPARENT = re.compile(r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}")
_CONTROL = re.compile("[\x00-\x09\x0b-\x1f\x7f-\x9f]")


def _id(prefix: str, n: int = 124) -> re.Pattern[str]:
    return re.compile(prefix + r"_[A-Za-z0-9_-]{1," + str(n) + "}")


FUL_ID = _id("ful")
SHP_ID = _id("shp")
RESULT_ID = re.compile(r"rsl_[0-9a-f]{32}")
ERROR_ID = re.compile(r"err_[0-9a-f]{32}")


def valid_timestamp(v: str) -> bool:
    m = _TS.fullmatch(v)
    if not m:
        return False
    y, mo, d, h, mi, s, _ms = (int(x) for x in m.groups())
    if not (1 <= mo <= 12 and 1970 <= y):
        return False
    return 1 <= d <= calendar.monthrange(y, mo)[1] and h < 24 and mi < 60 and s < 60


def _match(rx: re.Pattern[str]):
    return lambda v: rx.fullmatch(v) is not None


def _one_of(values):
    return lambda v: v in values


def _any(_v):
    return True


@dataclass(frozen=True)
class Field:
    name: str
    required: object  # bool, or callable(values) -> bool for conditional fields
    max: int
    rule: object


_F = Field
RECORDS: dict[str, tuple[Field, ...]] = {
    "HDR": (_F("magic", True, 0, None), _F("fileType", True, 3, _one_of(("REQ", "RES", "ERR"))),
            _F("createdAt", True, 24, valid_timestamp)),
    "REQ": (_F("fulfillmentRequestId", True, 128, _match(FUL_ID)), _F("orderId", True, 128, _match(_id("ord"))),
            _F("reservationId", True, 128, _match(_id("res"))), _F("siteId", True, 128, _match(_id("site", 123))),
            _F("origin", True, 5, _one_of(("EVENT", "API"))), _F("requestedAt", True, 24, valid_timestamp),
            _F("correlationId", False, 128, _any), _F("traceparent", False, 55, _match(_TRACEPARENT))),
    "LIN": (_F("lineNo", True, 2, _match(re.compile(r"[1-9][0-9]?"))), _F("sku", True, 80, _any),
            _F("quantity", True, 9, _match(re.compile(r"[1-9][0-9]{0,8}")))),
    "RES": (_F("resultId", True, 36, _match(RESULT_ID)), _F("fulfillmentRequestId", True, 128, _match(FUL_ID)),
            _F("revision", True, 6, _match(re.compile(r"[1-9][0-9]{0,5}"))), _F("status", True, 8, _one_of(STATUSES)),
            _F("orderId", True, 128, _match(_id("ord"))),
            _F("shipmentId", lambda r: r["status"] == "SHIPPED", 128, _match(SHP_ID)),
            _F("statusAt", True, 24, valid_timestamp),
            _F("reasonCode", lambda r: r["status"] == "REJECTED", 32, _one_of(REJECTION_CODES)),
            _F("reasonText", lambda r: r["status"] == "REJECTED", 200, _any),
            _F("correlationId", False, 128, _any), _F("traceparent", False, 55, _match(_TRACEPARENT))),
    "ERR": (_F("errorId", True, 36, _match(ERROR_ID)), _F("inboundFileName", True, 255, _any),
            _F("fulfillmentRequestId", False, 128, _match(FUL_ID)), _F("errorCode", True, 32, _one_of(ERROR_CODES)),
            _F("detail", True, 200, _any)),
    "TRL": (_F("recordCount", True, 3, _match(re.compile(r"[1-9][0-9]{0,2}"))),),
}


class ProtocolError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass
class ParsedFile:
    file_type: str
    records: list[dict[str, str]] = field(default_factory=list)

    @property
    def body(self) -> dict[str, str]:
        return self.records[1]

    @property
    def lines(self) -> list[dict[str, str]]:
        return [r for r in self.records if r["type"] == "LIN"]


def _split(line: str) -> list[str] | None:
    out, cur, i = [], [], 0
    while i < len(line):
        c = line[i]
        if c == "\\":
            nxt = line[i + 1] if i + 1 < len(line) else ""
            if nxt not in ("|", "\\"):
                return None
            cur.append(nxt)
            i += 2
            continue
        if c == "|":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return out


def parse(data: bytes, file_name: str) -> ParsedFile:
    """Parse and validate one ERP1 file. Raises ProtocolError with the SPEC section 3 code."""
    expected_type = next((t for t, rx in _NAMES.items() if rx.fullmatch(file_name)), None)
    if expected_type is None:
        raise ProtocolError("FILENAME_MISMATCH", f"not a ready name: {sanitize(file_name, 80)}")
    if len(data) > MAX_FILE_BYTES:
        raise ProtocolError("FILE_TOO_LARGE", f"{len(data)} bytes")
    if data.startswith(b"\xef\xbb\xbf"):
        raise ProtocolError("ENCODING", "byte-order mark")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ProtocolError("ENCODING", "invalid UTF-8") from exc
    if _CONTROL.search(text):
        raise ProtocolError("ENCODING", "control character")
    if not text.endswith("\n"):
        raise ProtocolError("TRUNCATED", "missing final LF")
    version = re.match(r"HDR\|ERP([0-9]+)(\||\n)", text)
    if not version:
        raise ProtocolError("MALFORMED_RECORD", "file does not start with HDR|ERP<n>")
    if version.group(1) != "1":
        raise ProtocolError("UNSUPPORTED_VERSION", f"ERP{version.group(1)}")

    parsed = ParsedFile(file_type="")
    done = False
    line_count = 0
    for line in text[:-1].split("\n"):
        rtype = line.split("|", 1)[0]
        fields = _split(line) if line else None
        if fields is None or rtype not in RECORDS or fields[0] != rtype:
            raise ProtocolError("MALFORMED_RECORD", f"record {len(parsed.records) + 1}")
        spec = RECORDS[rtype]
        if len(fields) - 1 != len(spec):
            raise ProtocolError("MALFORMED_RECORD", f"{rtype} has {len(fields) - 1} fields")
        n = len(parsed.records)
        if done:
            allowed: tuple[str, ...] = ()
        elif n == 0:
            allowed = ("HDR",)
        elif n == 1:
            allowed = (expected_type,)
        elif parsed.file_type == "REQ":
            allowed = ("LIN",) if line_count == 0 else ("LIN", "TRL") if line_count < MAX_LINES else ("TRL",)
        else:
            allowed = ("TRL",)
        if rtype not in allowed:
            raise ProtocolError("RECORD_ORDER", f"{rtype} at record {n + 1}")
        values: dict[str, str] = {"type": rtype}
        for f, v in zip(spec, fields[1:], strict=False):
            conditional = callable(f.required)
            required = f.required(values) if conditional else f.required
            if v == "":
                if required:
                    raise ProtocolError("MISSING_FIELD", f"{rtype}.{f.name}")
                values[f.name] = ""
                continue
            if f.max and len(v) > f.max:
                raise ProtocolError("FIELD_LENGTH", f"{rtype}.{f.name}")
            if conditional and not required:
                raise ProtocolError("FIELD_VALUE", f"{rtype}.{f.name} must be empty")
            if rtype == "HDR" and f.name == "fileType" and v != expected_type:
                raise ProtocolError("FIELD_VALUE", f"HDR.fileType {v} but the name is {expected_type}")
            if f.rule is not None and not f.rule(v):
                raise ProtocolError("FIELD_VALUE", f"{rtype}.{f.name}")
            values[f.name] = v
        if rtype == "HDR":
            parsed.file_type = values["fileType"]
        elif rtype == "LIN":
            line_count += 1
            if int(values["lineNo"]) != line_count:
                raise ProtocolError("FIELD_VALUE", f"LIN.lineNo {values['lineNo']} out of sequence")
        elif rtype == "TRL":
            done = True
        parsed.records.append(values)
    if not done:
        raise ProtocolError("TRUNCATED", "no TRL")
    if int(parsed.records[-1]["recordCount"]) != len(parsed.records):
        raise ProtocolError("COUNT_MISMATCH", f"TRL says {parsed.records[-1]['recordCount']}, found {len(parsed.records)}")
    if file_name != expected_name(parsed, file_name):
        raise ProtocolError("FILENAME_MISMATCH", file_name)
    return parsed


_REQ_NAME = re.compile(r"REQ\.(ful_[A-Za-z0-9_-]{1,124})\.([0-9a-f]{8,32})\.erp1")
_RES_NAME = re.compile(r"RES\.(ful_[A-Za-z0-9_-]{1,124})\.([0-9]{6})\.(rsl_[0-9a-f]{32})\.erp1")
_ERR_NAME = re.compile(r"ERR\.(err_[0-9a-f]{32})\.erp1")
_NAMES = {"REQ": _REQ_NAME, "RES": _RES_NAME, "ERR": _ERR_NAME}


def expected_name(parsed: ParsedFile, actual: str) -> str | None:
    """The name this file must have; for REQ the nonce is taken from the actual name if well formed."""
    b = parsed.body
    if parsed.file_type == "REQ":
        m = _REQ_NAME.fullmatch(actual)
        return f"REQ.{b['fulfillmentRequestId']}.{m.group(2)}.erp1" if m else None
    if parsed.file_type == "RES":
        return result_name(b["fulfillmentRequestId"], int(b["revision"]), b["resultId"])
    return error_name(b["errorId"])


def result_name(ful_id: str, revision: int, result_id: str) -> str:
    return f"RES.{ful_id}.{revision:06d}.{result_id}.erp1"


def error_name(error_id: str) -> str:
    return f"ERR.{error_id}.erp1"


def classify(area: str, name: str) -> str:
    """SPEC section 4: 'ready', 'ignore' (in progress) or 'quarantine'."""
    if name.startswith(".") and name.endswith(".tmp"):
        return "ignore"
    patterns = (_REQ_NAME,) if area == "inbound" else (_RES_NAME, _ERR_NAME)
    return "ready" if any(p.fullmatch(name) for p in patterns) else "quarantine"


def request_id_from_name(name: str) -> str | None:
    m = _REQ_NAME.fullmatch(name) or re.fullmatch(r"REQ\.(ful_[A-Za-z0-9_-]{1,124})\..*", name)
    return m.group(1) if m else None


def equivalence_key(parsed: ParsedFile) -> tuple:
    """SPEC section 6: the fields that define 'same content'."""
    r = parsed.body
    return (r["fulfillmentRequestId"], r["orderId"], r["reservationId"], r["siteId"],
            tuple((ln["lineNo"], ln["sku"], ln["quantity"]) for ln in parsed.lines))


def escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|")


def render(records: list[list[str]]) -> bytes:
    return "".join("|".join(escape(str(v)) for v in r) + "\n" for r in records).encode("utf-8")


def render_result(*, created_at: str, result_id: str, ful_id: str, revision: int, status: str, order_id: str,
                  shipment_id: str = "", status_at: str, reason_code: str = "", reason_text: str = "",
                  correlation_id: str = "", traceparent: str = "") -> bytes:
    return render([["HDR", MAGIC, "RES", created_at],
                   ["RES", result_id, ful_id, str(revision), status, order_id, shipment_id, status_at,
                    reason_code, reason_text, correlation_id, traceparent],
                   ["TRL", "3"]])


def render_error(*, created_at: str, error_id: str, inbound_name: str, ful_id: str, code: str, detail: str) -> bytes:
    return render([["HDR", MAGIC, "ERR", created_at],
                   ["ERR", error_id, sanitize(inbound_name, 255), ful_id or "", code, sanitize(detail, 200)],
                   ["TRL", "3"]])


def sanitize(value: str, limit: int) -> str:
    """Make arbitrary text safe for a free-text field (SPEC section 2, ERR.inboundFileName):
    invalid UTF-8 (surrogate escapes from os.listdir) becomes U+FFFD, control characters
    become '?', and the result keeps the first `limit` code points."""
    value = value.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    return _CONTROL.sub("?", value)[:limit] or "-"
