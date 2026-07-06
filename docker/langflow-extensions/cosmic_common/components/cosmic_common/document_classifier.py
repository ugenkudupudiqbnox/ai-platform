"""Document classifier component (constitution §4).

Generic, reusable, **deterministic** classifier that labels a document
(invoice / receipt / credit_note / payment / unknown) with a confidence score.
v1 is rule-based (keyword scoring over the extracted content); an LLM path is a
documented hook (see ADR-0004). Per §4 (fail safe over fail fast), low confidence
returns ``code=AR_UNCERTAIN`` so the caller escalates rather than guesses.

Never raises (§5/§9): unparseable content / no hits surface as
``AR_UNCERTAIN`` with ``doc_type="unknown"``, never as an exception.
"""

import json
import re

from lfx.custom import Component
from lfx.io import FloatInput, MessageTextInput, MultilineInput, Output
from lfx.schema import Message


def _envelope(status: str, code: str, data: dict | None = None,
              error: dict | None = None) -> dict:
    env: dict = {"status": status, "code": code, "data": data or {}}
    if error:
        env["error"] = error
    return env


# Keyword rules: type -> list of (regex, weight). Lowercased content is scanned.
# Weights reflect how strongly a token implies a doc type (e.g. "credit note"
# is near-decisive; "amount" is weak and shared, so excluded).
RULES: dict[str, list[tuple[str, int]]] = {
    "invoice": [
        (r"\binvoice\b", 2),
        (r"\btax\s+invoice\b", 3),
        # "invoice number", "invoice_number", "invoice_date", "invoice_id",
        # "invoice no", "invoice #" — the underscore form is the common CSV/XLSX
        # header convention; `\b` alone won't match it because `_` is a word char.
        (r"\binvoice[_\s]+(no|number|#|num|date|id)\b", 3),
        # "INV-1001", "INV#1001", "INV 1001", "INV1001" — hyphen/space/#/none
        # before the digits. The prior `\binv\s*#?\d` missed the hyphenated form.
        (r"\binv[-\s#]*\d", 2),
        (r"\bbill\s+to\b", 1),
        (r"\bsub[\s-]?total\b", 1),
        (r"\bamount\s+due\b", 2),
        (r"\bnet\s+total\b", 1),
        (r"\bP[Oo]\s*(number|no|#)\b", 1),
    ],
    "receipt": [
        (r"\breceipt\b", 3),
        (r"\bpayment\s+received\b", 2),
        (r"\bpos\s+receipt\b", 3),
        (r"\bcashier\b", 1),
        (r"\bchange\s+due\b", 2),
        (r"\bthank\s+you\b", 1),
        (r"\btransaction\s+id\b", 1),
        (r"\bfoodics\b", 1),
    ],
    "credit_note": [
        (r"\bcredit\s+note\b", 3),
        (r"\bcredit\s+memo\b", 3),
        (r"\bcn\s+(no|number|#)\b", 2),
        (r"\bcredit\s+note\s+(no|number|#)\b", 3),
        (r"\breturned\s+goods\b", 1),
    ],
    "payment": [
        (r"\bpayment\s+advice\b", 3),
        (r"\bremittance\b", 3),
        (r"\bpayment\s+ref(erence)?\b", 2),
        (r"\bpaid\s+amount\b", 1),
        (r"\bremit\b", 2),
        (r"\bwire\s+transfer\b", 1),
    ],
}


def _extract_text(content_ref: str) -> str:
    """Coerce ``content_ref`` (plain text, or a reader envelope JSON) to text."""
    if not content_ref:
        return ""
    s = content_ref.strip()
    # Reader envelopes are JSON envelopes with data.rows / data.pages.
    if s.startswith("{"):
        try:
            obj = json.loads(s)
        except (ValueError, TypeError):
            return s
        data = obj.get("data", obj) if isinstance(obj, dict) else {}
        if not isinstance(data, dict):
            return s
        parts: list[str] = []
        rows = data.get("rows")
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    parts.extend(str(v) for v in row.values())
                elif isinstance(row, list):
                    parts.extend(str(v) for v in row)
                else:
                    parts.append(str(row))
        pages = data.get("pages")
        if isinstance(pages, list):
            for pg in pages:
                if isinstance(pg, dict):
                    parts.append(str(pg.get("text", "")))
        tables = data.get("tables")
        if isinstance(tables, list):
            for tbl in tables:
                if isinstance(tbl, dict):
                    for row in tbl.get("rows", []):
                        if isinstance(row, list):
                            parts.extend(str(c) for c in row)
        return " ".join(parts)
    return s


class DocumentClassifierComponent(Component):
    name = "DocumentClassifierComponent"
    display_name = "Document Classifier"
    description = (
        "Classify a document as invoice / receipt / credit_note / payment / "
        "unknown with a confidence score. Call this to route an ingested "
        "document to the right subflow before matching or posting."
    )
    icon = "Tag"

    inputs = [
        MessageTextInput(
            name="document_ref",
            display_name="Document Ref",
            info="Stable id of the document being classified.",
            required=True,
            tool_mode=True,
        ),
        MessageTextInput(
            name="content_ref",
            display_name="Content Ref",
            info="Extracted text/rows (plain text or a reader envelope JSON).",
            required=True,
            tool_mode=True,
        ),
        MultilineInput(
            name="candidate_types",
            display_name="Candidate Types",
            info="One candidate type per line (default: invoice, receipt, credit_note, payment).",
            tool_mode=True,
        ),
        MessageTextInput(
            name="rules_ref",
            display_name="Rules Ref",
            info="Optional reference to a rule set overriding the defaults (unused in v1).",
            tool_mode=True,
        ),
        FloatInput(
            name="min_confidence",
            display_name="Min Confidence",
            value=0.8,
            info="Below this confidence the result is AR_UNCERTAIN (§4 fail-safe).",
        ),
    ]

    outputs = [
        Output(
            name="classifier_output",
            display_name="Classification",
            method="classify",
        ),
    ]

    def classify(self) -> Message:
        document_ref = (self.document_ref or "").strip()
        try:
            min_conf = float(self.min_confidence or 0.8)
        except (TypeError, ValueError):
            min_conf = 0.8
        candidates_raw = (self.candidate_types or "").strip()
        if candidates_raw:
            candidates = [c.strip() for c in candidates_raw.splitlines()
                         if c.strip()]
        else:
            candidates = list(RULES.keys())
        text = _extract_text(self.content_ref or "").lower()
        scores: dict[str, int] = {}
        for cand in candidates:
            rules = RULES.get(cand, [])
            score = 0
            for pattern, weight in rules:
                if re.search(pattern, text):
                    score += weight
            scores[cand] = score
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        best_type, best_score = ranked[0] if ranked else ("unknown", 0)
        second_score = ranked[1][1] if len(ranked) > 1 else 0
        if best_score <= 0:
            doc_type = "unknown"
            confidence = 0.0
        else:
            doc_type = best_type
            # Dominance-based confidence: best vs best+second. A clear winner
            # (second_score == 0) → 1.0; a tie → 0.5.
            confidence = best_score / (best_score + second_score) \
                if (best_score + second_score) > 0 else 0.0
        code = "AR_UNCERTAIN" if confidence < min_conf else "AR_OK"
        status = "pending_approval" if code == "AR_UNCERTAIN" else "ok"
        # AR_UNCERTAIN is read-only/uncertain, not an approval pause; surface as
        # ok-with-code so the orchestrator can escalate per §4. Keep status=ok
        # but the code carries the fail-safe signal.
        status = "ok"
        return Message(text=json.dumps(_envelope(
            status, code,
            data={"document": document_ref, "doc_type": doc_type,
                  "confidence": round(confidence, 4),
                  "candidate_types": {k: v for k, v in scores.items()},
                  "min_confidence": min_conf})))