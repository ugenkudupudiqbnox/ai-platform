#!/usr/bin/env python3
"""adapter_selftest — offline stdlib-only tests for the OpenAI adapter's pure
functions.

Covers the Cosmic AR additions (constitution §14/§19): file extraction from
OpenAI/LibreChat messages, data-URL decoding, §14 envelope parsing, approval-ref
detection, and pending-approval rendering. No network, no LangFlow, no Docker —
`python3 adapter_selftest.py` runs anywhere. Mirrors the repo's self-test
convention (CLAUDE.md): a tiny harness with PASS/FAIL counts that exits non-zero
on any failure, so `make test` (via scripts/adapter.selftest.sh) and CI pick it
up.

Run:  python3 docker/langflow-adapter/adapter_selftest.py
"""
import base64
import os
import sys

# Make the sibling adapter.py importable regardless of the cwd we're run from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adapter  # noqa: E402

PASS = 0
FAIL = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  \033[32mPASS\033[0m {name}")


def bad(name, detail=""):
    global FAIL
    FAIL += 1
    print(f"  \033[31mFAIL\033[0m {name}" + (f" — {detail}" if detail else ""))


def eq(got, expected, name):
    if got == expected:
        ok(name)
    else:
        bad(name, f"expected {expected!r}, got {got!r}")


def truthy(value, name):
    ok(name) if value else bad(name, f"expected truthy, got {value!r}")


def falsy(value, name):
    ok(name) if not value else bad(name, f"expected falsy, got {value!r}")


def _data_url(mime, payload):
    return f"data:{mime};base64,{base64.b64encode(payload).decode()}"


PNG = b"\x89PNG\r\n\x1a\n"  # tiny fake png bytes


# --------------------------------------------------------------------------- #
#  _decode_data_url
# --------------------------------------------------------------------------- #
print("[1] _decode_data_url")
dec = adapter._decode_data_url(_data_url("image/png", PNG))
if isinstance(dec, tuple) and dec[0] == PNG and dec[1] == "image/png":
    ok("data URL decodes to (bytes, mime)")
else:
    bad("data URL decode", repr(dec))

eq(adapter._decode_data_url("https://example.com/a.png"), None, "remote url → None (no fetch / no SSRF)")
eq(adapter._decode_data_url("data:text/plain,hello"), None, "non-base64 data url → None")
eq(adapter._decode_data_url("not a url"), None, "non-data string → None")
eq(adapter._decode_data_url(None), None, "None → None")
eq(adapter._decode_data_url("data:image/png;base64,!!!not-base64!!!"), None,
   "malformed base64 → None (validate=True rejects garbage)")

# --------------------------------------------------------------------------- #
#  extract_files
# --------------------------------------------------------------------------- #
print("[2] extract_files")
eq(adapter.extract_files([]), [], "empty messages → []")
eq(adapter.extract_files(None), [], "None messages → []")

# LibreChat attachment (data URL)
msgs = [{"role": "user", "content": "hi",
         "attachments": [{"filename": "inv.xlsx", "contentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "url": _data_url("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", b"xlsx-bytes")}]}]
files = adapter.extract_files(msgs)
eq(len(files), 1, "librechat attachment: one file extracted")
if files:
    eq(files[0][0], "inv.xlsx", "librechat attachment: filename")
    eq(files[0][1], b"xlsx-bytes", "librechat attachment: bytes")
    eq(files[0][2], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "librechat attachment: content_type")

# OpenAI image_url content part (data URL)
msgs = [{"role": "user", "content": [
    {"type": "text", "text": "match this receipt"},
    {"type": "image_url", "image_url": {"url": _data_url("image/png", PNG)}},
]}]
files = adapter.extract_files(msgs)
eq(len(files), 1, "image_url part: one file extracted (text part ignored)")
if files:
    truthy(files[0][0].endswith(".png"), "image_url part: filename has .png ext")
    eq(files[0][1], PNG, "image_url part: bytes")
    eq(files[0][2], "image/png", "image_url part: content_type")

# input_image / output_image variants
msgs = [{"role": "user", "content": [{"type": "input_image", "image_url": _data_url("image/jpeg", b"jpg")}]}]
files = adapter.extract_files(msgs)
eq(len(files), 1, "input_image part: one file extracted")
if files:
    eq(files[0][1], b"jpg", "input_image part: bytes")

# Remote URL must NOT be fetched (no SSRF surface)
msgs = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.com/secret.png"}}]}]
eq(adapter.extract_files(msgs), [], "remote http(s) url skipped (no fetch)")

# input_file with data URL
msgs = [{"role": "user", "content": [{"type": "input_file", "filename": "r.csv", "file_data": {"url": _data_url("text/csv", b"a,b\n1,2")}}]}]
files = adapter.extract_files(msgs)
eq(len(files), 1, "input_file part: one file extracted")
if files:
    eq(files[0][0], "r.csv", "input_file part: filename")
    eq(files[0][1], b"a,b\n1,2", "input_file part: bytes")

# Mixed: attachment + image part + text → both files, text ignored
msgs = [{"role": "user", "content": [{"type": "text", "text": "see attached"}],
         "attachments": [{"filename": "a1.pdf", "url": _data_url("application/pdf", b"%PDF-1")}],
         }]
files = adapter.extract_files(msgs)
eq(len(files), 1, "attachment-only message: one file")

# --------------------------------------------------------------------------- #
#  parse_envelope
# --------------------------------------------------------------------------- #
print("[3] parse_envelope")
env = {"status": "pending_approval", "code": "AR_APPROVAL_REQUIRED", "approval_ref": "ar-approval-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "data": {"action": "ar_issue_invoice"}}
eq(adapter.parse_envelope('{"status":"pending_approval","code":"AR_APPROVAL_REQUIRED","approval_ref":"ar-approval-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee","data":{"action":"ar_issue_invoice"}}'), env, "valid envelope dict")
eq(adapter.parse_envelope("plain human answer"), None, "plain text → None")
eq(adapter.parse_envelope("[1,2,3]"), None, "json array → None")
eq(adapter.parse_envelope("42"), None, "json number → None")
eq(adapter.parse_envelope(""), None, "empty string → None")
eq(adapter.parse_envelope(None), None, "None → None")
eq(adapter.parse_envelope("{not valid json"), None, "malformed json → None")

# --------------------------------------------------------------------------- #
#  detect_approval_reply
# --------------------------------------------------------------------------- #
print("[4] detect_approval_reply")
ref = "ar-approval-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
eq(adapter.detect_approval_reply(f"approve {ref}"), ref, "approval reply detected")
eq(adapter.detect_approval_reply(f"please approve ar-approval-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee now"), ref, "ref embedded in text")
eq(adapter.detect_approval_reply("nothing to see here"), None, "no ref → None")
eq(adapter.detect_approval_reply(""), None, "empty → None")
eq(adapter.detect_approval_reply(None), None, "None → None")
# The ref regex must match exactly 36 chars after the prefix (uuid shape); a
# truncated ref must NOT match (prevents resuming on a partial paste).
eq(adapter.detect_approval_reply("approve ar-approval-aaaaaaaa"), None, "truncated ref → None (no partial match)")

# --------------------------------------------------------------------------- #
#  render_approval
# --------------------------------------------------------------------------- #
print("[5] render_approval")
prompt, meta = adapter.render_approval(env)
truthy(isinstance(prompt, str) and ref in prompt, "pending_approval → prompt mentions the ref")
truthy(prompt and "approve" in prompt and "reject" in prompt, "prompt offers approve/reject")
eq(meta, {"status": "pending_approval", "approval_ref": ref, "action": "ar_issue_invoice", "checkpoint_id": ""}, "x_cosmic_approval metadata shape")
# Non-pending envelope → (None, None)
eq(adapter.render_approval({"status": "ok", "code": "AR_OK"}), (None, None), "ok envelope → (None, None)")
eq(adapter.render_approval(None), (None, None), "None → (None, None)")
# Missing action falls back to intent
p2, m2 = adapter.render_approval({"status": "pending_approval", "approval_ref": ref, "intent": "ar_audit"})
eq(m2["action"], "ar_audit", "missing data.action falls back to intent")
# checkpoint_id from top-level
p3, m3 = adapter.render_approval({"status": "pending_approval", "approval_ref": ref, "checkpoint_id": "ckpt-123"})
eq(m3["checkpoint_id"], "ckpt-123", "checkpoint_id read from top-level")

# --------------------------------------------------------------------------- #
#  Summary
# --------------------------------------------------------------------------- #
print(f"\n== results: {PASS} passed, {FAIL} failed ==")
sys.exit(1 if FAIL else 0)