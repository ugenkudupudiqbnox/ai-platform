"""CSV reader component (constitution §15/§16).

Generic, reusable reader for CSV files (stdlib ``csv`` — no build-phase dep).
Parses the file with the chosen delimiter and emits the rows in the §14
envelope (``data.rows`` = list of dict rows when ``has_header``, else list of
lists), shaped toward ``DocumentManifest``. Custom reader vs the LangFlow
built-in for contract-conformant output + §16 PII rules (see ADR-0002).

Never raises (§5/§9): file-not-found / corrupt-file / parse errors surface as
``code=AR_VALIDATION`` envelopes, never as exceptions out of the output method.
"""

import csv
import json
import os

from lfx.custom import Component
from lfx.io import BoolInput, IntInput, MessageTextInput, Output
from lfx.schema import Message


def _envelope(status: str, code: str, data: dict | None = None,
              error: dict | None = None) -> dict:
    env: dict = {"status": status, "code": code, "data": data or {}}
    if error:
        env["error"] = error
    return env


class CSVReaderComponent(Component):
    name = "CSVReaderComponent"
    display_name = "CSV Reader"
    description = (
        "Read a CSV file and return its rows in the canonical envelope. Call "
        "this when the user provides a CSV export of invoices, payments, or "
        "POS receipts that must be ingested."
    )
    icon = "FileText"

    inputs = [
        MessageTextInput(
            name="file_path",
            display_name="File Path",
            info="Path to the CSV file (resolved inside the LangFlow container).",
            required=True,
            tool_mode=True,
        ),
        MessageTextInput(
            name="delimiter",
            display_name="Delimiter",
            value=",",
            info="Field delimiter (default ','). Use '\\t' for TSV.",
        ),
        BoolInput(
            name="has_header",
            display_name="First Row Is Header",
            value=True,
            info="Treat the first row as column headers.",
        ),
        IntInput(
            name="max_rows",
            display_name="Max Rows",
            value=0,
            info="Maximum rows to return (0 = no limit).",
        ),
    ]

    outputs = [
        Output(
            name="reader_output",
            display_name="Rows",
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
                error={"message": f"file not found: {file_path}"},
                )))
        delim = self.delimiter or ","
        if delim == "\\t":
            delim = "\t"
        has_header = bool(self.has_header) if self.has_header is not None else True
        try:
            max_rows = int(self.max_rows or 0)
        except (TypeError, ValueError):
            max_rows = 0
        try:
            with open(file_path, newline="", encoding="utf-8",
                      errors="replace") as fh:
                if has_header:
                    reader = csv.DictReader(fh, delimiter=delim)
                    rows: list = []
                    for i, row in enumerate(reader):
                        if max_rows and i >= max_rows:
                            break
                        rows.append({str(k): ("" if v is None else str(v))
                                     for k, v in row.items() if k is not None})
                else:
                    reader = csv.reader(fh, delimiter=delim)
                    rows = []
                    for i, row in enumerate(reader):
                        if max_rows and i >= max_rows:
                            break
                        rows.append(["" if c is None else str(c) for c in row])
        except (csv.Error, OSError, UnicodeDecodeError) as exc:
            return Message(text=json.dumps(_envelope(
                "error", "AR_VALIDATION",
                error={"message": f"CSV parse failed: {exc}"})))
        return Message(text=json.dumps(_envelope(
            "ok", "AR_OK",
            data={"file": file_path, "rows": rows, "row_count": len(rows),
                  "has_header": has_header, "delimiter": delim})))