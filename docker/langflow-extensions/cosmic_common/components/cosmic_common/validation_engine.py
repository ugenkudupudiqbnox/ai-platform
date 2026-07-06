"""Validation engine component (constitution §9).

Generic, reusable validator that checks a payload against one of the project's
JSON-Schema contracts and emits a ``ValidationResult``. v1 implements the
``DocumentManifest`` contract **by hand** (the schema's regex/enum/required rules
embedded as pure functions, plus a totals cross-check) so the File Intake Flow
can validate without ``jsonschema`` installed or the contracts dir mounted in
the container (see ADR-0004 §5). The other 13 contracts stay
``AR_NOT_IMPLEMENTED`` — they belong to other subflows.

Never raises (§5/§9): all validation failures are collected into ``data.errors``
and the envelope ``code`` is ``AR_VALIDATION`` when invalid; the output method
itself never raises.
"""

import json
import re
from decimal import Decimal, InvalidOperation

from lfx.custom import Component
from lfx.io import DropdownInput, MessageTextInput, MultilineInput, Output
from lfx.schema import Message


def _envelope(status: str, code: str, data: dict | None = None,
              error: dict | None = None) -> dict:
    env: dict = {"status": status, "code": code, "data": data or {}}
    if error:
        env["error"] = error
    return env


RE_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
RE_AMOUNT = re.compile(r"^-?\d+\.\d{2}$")
RE_CURRENCY = re.compile(r"^[A-Z]{3}$")
RE_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RE_VERSION = re.compile(r"^\d+\.\d+\.\d+$")

DOC_TYPES = {"invoice", "receipt", "credit_note", "payment"}
SOURCES = {"zoho", "foodics"}

MANIFEST_REQUIRED = ["manifest_id", "trace_id", "tenant", "documents",
                     "totals", "contract_version"]
DOC_REQUIRED = ["doc_id", "doc_type", "source", "source_ref", "customer_ref",
                 "amount", "currency", "posted_at", "status", "fetched_at"]
TOTALS_REQUIRED = ["count", "sum"]


def validate_document(doc: dict, prefix: str = "") -> tuple[list[str], list[Decimal]]:
    """Pure per-document validator for the ``DocumentManifest.document`` contract.

    Returns ``(errors, amounts)`` where ``amounts`` is the list of parsed
    ``Decimal`` amounts (empty entry on malformed/missing amount) for the
    manifest totals cross-check. ``prefix`` prefixes each error string (e.g.
    ``"documents[2]"``). Mirrors the schema's required/enum/pattern rules +
    ``additionalProperties:false``. No external deps — reused by the File Intake
    Flow's ``validate`` node (§15) so per-document validation isn't duplicated.
    """
    errs: list[str] = []
    amounts: list[Decimal] = []
    p = f"{prefix}." if prefix else ""
    if not isinstance(doc, dict):
        return [f"{prefix or 'document'}: must be an object"], amounts
    for k in doc:
        if k not in DOC_REQUIRED:
            errs.append(f"{prefix}: unexpected property '{k}'")
    for k in DOC_REQUIRED:
        if k not in doc:
            errs.append(f"{prefix}: missing required property '{k}'")
    if "doc_type" in doc and doc["doc_type"] not in DOC_TYPES:
        errs.append(f"{p}doc_type: must be one of {sorted(DOC_TYPES)}")
    if "source" in doc and doc["source"] not in SOURCES:
        errs.append(f"{p}source: must be one of {sorted(SOURCES)}")
    for fld, pat in (("amount", RE_AMOUNT), ("currency", RE_CURRENCY),
                     ("posted_at", RE_TS), ("fetched_at", RE_TS)):
        if fld in doc and not (isinstance(doc[fld], str) and pat.match(doc[fld])):
            errs.append(f"{p}{fld}: invalid format")
    for fld in ("doc_id", "source_ref", "customer_ref", "status"):
        if fld in doc and not (isinstance(doc[fld], str) and doc[fld]):
            errs.append(f"{p}{fld}: must be a non-empty string")
    if "amount" in doc and isinstance(doc["amount"], str) \
            and RE_AMOUNT.match(doc["amount"]):
        try:
            amounts.append(Decimal(doc["amount"]))
        except InvalidOperation:
            errs.append(f"{p}amount: unparseable decimal")
    return errs, amounts


def validate_document_manifest(payload: dict) -> list[str]:
    """Pure validator for the DocumentManifest contract.

    Returns a list of human-readable error strings (empty = valid). Mirrors the
    schema's required/enum/pattern rules + additionalProperties:false, plus a
    totals cross-check (``sum`` == Σ document amounts to 2dp). No external deps.
    """
    errs: list[str] = []
    if not isinstance(payload, dict):
        return ["payload: must be a JSON object"]
    # additionalProperties:false — flag unknown top-level keys.
    allowed_top = set(MANIFEST_REQUIRED) | {"source_systems", "period",
                                            "generated_at"}
    for k in payload:
        if k not in allowed_top:
            errs.append(f"root: unexpected property '{k}'")
    for k in MANIFEST_REQUIRED:
        if k not in payload:
            errs.append(f"root: missing required property '{k}'")
    if "manifest_id" in payload and not (
            isinstance(payload["manifest_id"], str) and RE_UUID.match(
                payload["manifest_id"])):
        errs.append("manifest_id: must be a uuid")
    if "trace_id" in payload and not (isinstance(payload["trace_id"], str)
                                      and payload["trace_id"]):
        errs.append("trace_id: must be a non-empty string")
    if "tenant" in payload and not (isinstance(payload["tenant"], str)
                                    and payload["tenant"]):
        errs.append("tenant: must be a non-empty string")
    if "contract_version" in payload and not (
            isinstance(payload["contract_version"], str)
            and RE_VERSION.match(payload["contract_version"])):
        errs.append("contract_version: must match \\d+\\.\\d+\\.\\d+")
    if "generated_at" in payload and not (
            isinstance(payload["generated_at"], str)
            and RE_TS.match(payload["generated_at"])):
        errs.append("generated_at: must be an ISO-8601 UTC timestamp")
    docs = payload.get("documents")
    if "documents" in payload:
        if not isinstance(docs, list):
            errs.append("documents: must be an array")
            docs = []
        sums: list[Decimal] = []
        for i, d in enumerate(docs or []):
            d_errs, d_amounts = validate_document(d, prefix=f"documents[{i}]")
            errs.extend(d_errs)
            sums.extend(d_amounts)
    else:
        sums = []
    totals = payload.get("totals")
    if "totals" in payload:
        if not isinstance(totals, dict):
            errs.append("totals: must be an object")
            totals = {}
        for k in totals:
            if k not in TOTALS_REQUIRED:
                errs.append(f"totals: unexpected property '{k}'")
        for k in TOTALS_REQUIRED:
            if k not in totals:
                errs.append(f"totals: missing required property '{k}'")
        if "count" in totals and not (isinstance(totals["count"], int)
                                      and totals["count"] >= 0):
            errs.append("totals.count: must be a non-negative integer")
        if "sum" in totals:
            if not (isinstance(totals["sum"], str)
                    and RE_AMOUNT.match(totals["sum"])):
                errs.append("totals.sum: must be a 2dp signed string")
            else:
                try:
                    expected = sum(sums, Decimal("0.00")).quantize(
                        Decimal("0.01"))
                    actual = Decimal(totals["sum"])
                    if actual != expected:
                        errs.append(
                            f"totals.sum: cross-check failed — "
                            f"expected {expected}, got {actual}")
                except InvalidOperation:
                    errs.append("totals.sum: unparseable decimal")
                except (KeyError, TypeError) as exc:
                    errs.append(f"totals.sum: cross-check error: {exc}")
        # count cross-check
        if "count" in totals and isinstance(totals["count"], int) \
                and "documents" in payload and isinstance(docs, list):
            if totals["count"] != len(docs):
                errs.append(
                    f"totals.count: cross-check failed — documents has "
                    f"{len(docs)} entries, count={totals['count']}")
    source_systems = payload.get("source_systems")
    if "source_systems" in payload:
        if not isinstance(source_systems, list):
            errs.append("source_systems: must be an array")
        else:
            for s in source_systems:
                if s not in SOURCES:
                    errs.append(f"source_systems: invalid member '{s}'")
            if isinstance(source_systems, list):
                distinct = set(source_systems)
                if "documents" in payload and isinstance(docs, list):
                    actual = {d.get("source") for d in docs
                             if isinstance(d, dict) and "source" in d}
                    if distinct != actual:
                        errs.append(
                            f"source_systems: must equal the distinct sources "
                            f"in documents — got {sorted(distinct)}, "
                            f"expected {sorted(actual)}")
    period = payload.get("period")
    if "period" in payload:
        if not isinstance(period, dict):
            errs.append("period: must be an object")
        else:
            for k in period:
                if k not in ("start", "end"):
                    errs.append(f"period: unexpected property '{k}'")
            for k in ("start", "end"):
                if k not in period:
                    errs.append(f"period: missing required property '{k}'")
                elif not (isinstance(period[k], str) and RE_DATE.match(
                        period[k])):
                    errs.append(f"period.{k}: must be a YYYY-MM-DD date")
    return errs


CONTRACT_VERSIONS: dict[str, str] = {
    "DocumentManifest": "1.0.0",
}


class ValidationEngineComponent(Component):
    name = "ValidationEngineComponent"
    display_name = "Validation Engine"
    description = (
        "Validate a JSON payload against one of the project's contracts and "
        "return a ValidationResult. Call this before posting to Zoho or "
        "persisting state, to guarantee the payload conforms (§8/§9)."
    )
    icon = "ShieldCheck"

    inputs = [
        DropdownInput(
            name="contract_name",
            display_name="Contract",
            options=[
                "WorkflowState",
                "DocumentManifest",
                "RevenueData",
                "CollectionData",
                "ExpenseData",
                "ValidationResult",
                "CalculationResult",
                "InvoiceData",
                "ApprovalRequest",
                "ApprovalResult",
                "ZohoUploadResult",
                "AuditRecord",
                "Notification",
                "ExecutionSummary",
            ],
            value="InvoiceData",
            info="The contract to validate against (see cosmic-ar/contracts/registry.json).",
            tool_mode=True,
        ),
        MultilineInput(
            name="payload",
            display_name="Payload (JSON)",
            info="JSON payload to validate.",
            required=True,
            tool_mode=True,
        ),
        MessageTextInput(
            name="trace_id",
            display_name="Trace ID",
            info="Correlation id propagated into the ValidationResult (§12).",
            tool_mode=True,
        ),
    ]

    outputs = [
        Output(
            name="validation_output",
            display_name="Validation Result",
            method="validate",
        ),
    ]

    def validate(self) -> Message:
        contract = self.contract_name or "InvoiceData"
        trace_id = (self.trace_id or "").strip()
        raw = self.payload or ""
        if isinstance(raw, str):
            raw_s = raw.strip()
        else:
            raw_s = json.dumps(raw)
        if contract != "DocumentManifest":
            return Message(text=json.dumps(_envelope(
                "ok", "AR_NOT_IMPLEMENTED",
                data={"valid": False, "contract_name": contract,
                      "contract_version": CONTRACT_VERSIONS.get(contract, ""),
                      "errors": [f"validation for {contract} is build-phase"],
                      "trace_id": trace_id})))
        # Parse payload.
        try:
            payload = json.loads(raw_s)
        except (ValueError, TypeError) as exc:
            return Message(text=json.dumps(_envelope(
                "error", "AR_VALIDATION",
                error={"message": f"payload is not valid JSON: {exc}"})))
        errs = validate_document_manifest(payload)
        valid = not errs
        code = "AR_OK" if valid else "AR_VALIDATION"
        return Message(text=json.dumps(_envelope(
            "ok", code,
            data={"valid": valid, "contract_name": contract,
                  "contract_version": CONTRACT_VERSIONS["DocumentManifest"],
                  "errors": errs, "trace_id": trace_id})))