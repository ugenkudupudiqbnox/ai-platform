"""PDF reader component (constitution §15/§16).

Generic, reusable reader for PDF files. Extracts text (and optionally tables)
with ``pdfplumber`` and emits the content in the §14 envelope
(``data.pages`` = list of ``{"page": N, "text": "..."}``,
``data.tables`` = list of ``{"page": N, "rows": [[...], ...]}``), shaped toward
``DocumentManifest``. Custom reader vs the LangFlow built-in for
contract-conformant output + §16 PII rules (see ADR-0002).

``pdfplumber`` is baked into ``docker/langflow/Dockerfile`` (see ADR-0004); if it
is not importable the reader returns ``code=AR_NOT_IMPLEMENTED`` rather than
raising. Never raises (§5/§9): file-not-found / corrupt-file / parse errors
surface as ``code=AR_VALIDATION`` envelopes.
"""

import json
import os
import re

from lfx.custom import Component
from lfx.io import BoolInput, MessageTextInput, Output
from lfx.schema import Message


def _envelope(status: str, code: str, data: dict | None = None,
              error: dict | None = None) -> dict:
    env: dict = {"status": status, "code": code, "data": data or {}}
    if error:
        env["error"] = error
    return env


def _parse_pages(spec: str, total: int) -> list[int]:
    """Resolve a page spec like '1-5', '3', '1,3,5', '' (all) → 0-based indices.

    Pages are 1-based in the spec and clamped to ``total``.
    """
    if not spec or not spec.strip():
        return list(range(total))
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            try:
                lo_i = max(1, int(lo))
                hi_i = min(total, int(hi))
            except ValueError:
                continue
            out.extend(range(lo_i - 1, hi_i))
        else:
            try:
                idx = int(part) - 1
            except ValueError:
                continue
            if 0 <= idx < total:
                out.append(idx)
    # de-dup, keep order
    seen: set[int] = set()
    uniq: list[int] = []
    for i in out:
        if i not in seen:
            seen.add(i)
            uniq.append(i)
    return uniq


class PDFReaderComponent(Component):
    name = "PDFReaderComponent"
    display_name = "PDF Reader"
    description = (
        "Read a PDF (extract text and optionally tables) and return its content "
        "in the canonical envelope. Call this when the user uploads a scanned "
        "invoice or statement that must be parsed before matching."
    )
    icon = "FileType"

    inputs = [
        MessageTextInput(
            name="file_path",
            display_name="File Path",
            info="Path to the PDF file (resolved inside the LangFlow container).",
            required=True,
            tool_mode=True,
        ),
        MessageTextInput(
            name="pages",
            display_name="Pages",
            info="Page range to read, e.g. '1-5' (blank = all pages).",
            tool_mode=True,
        ),
        BoolInput(
            name="extract_tables",
            display_name="Extract Tables",
            value=False,
            info="Also extract tables (slower) in addition to text.",
        ),
    ]

    outputs = [
        Output(
            name="reader_output",
            display_name="Content",
            method="read",
        ),
    ]

    def read(self) -> Message:
        file_path = (self.file_path or "").strip()
        if not file_path:
            return Message(text=json.dumps(_envelope(
                "error", "AR_VALIDATION",
                error={"message": "file_path is required"})))
        if not os.path.isfile(file_path):
            return Message(text=json.dumps(_envelope(
                "error", "AR_VALIDATION",
                error={"message": f"file not found: {file_path}"})))
        try:
            import pdfplumber
        except ImportError as exc:
            return Message(text=json.dumps(_envelope(
                "error", "AR_NOT_IMPLEMENTED",
                error={"message": f"pdfplumber not available in image: {exc}"}
            )))
        pages_spec = (self.pages or "").strip()
        extract_tables = bool(self.extract_tables)
        try:
            with pdfplumber.open(file_path) as pdf:
                total = len(pdf.pages)
                indices = _parse_pages(pages_spec, total)
                page_objs = [pdf.pages[i] for i in indices if i < total]
                out_pages = []
                out_tables = []
                for i, pg in enumerate(page_objs):
                    text = pg.extract_text() or ""
                    out_pages.append({"page": indices[i] + 1, "text": text})
                    if extract_tables:
                        try:
                            tbls = pg.extract_tables() or []
                        except Exception:  # noqa: BLE001 — table extraction is best-effort
                            tbls = []
                        for tbl in tbls:
                            rows = [[("" if c is None else str(c)) for c in row]
                                    for row in tbl]
                            out_tables.append(
                                {"page": indices[i] + 1, "rows": rows})
        except OSError as exc:
            return Message(text=json.dumps(_envelope(
                "error", "AR_VALIDATION",
                error={"message": f"PDF open failed: {exc}"})))
        except Exception as exc:  # noqa: BLE001 — pdfplumber raises generic errors on corrupt files
            msg = str(exc)
            # Drop raw tracebacks/PII from the surfaced message (§12).
            msg = re.sub(r"\s+", " ", msg)[:200]
            return Message(text=json.dumps(_envelope(
                "error", "AR_VALIDATION",
                error={"message": f"PDF parse failed: {msg}"})))
        return Message(text=json.dumps(_envelope(
            "ok", "AR_OK",
            data={"file": file_path, "pages": out_pages,
                  "tables": out_tables,
                  "page_count": len(out_pages)})))