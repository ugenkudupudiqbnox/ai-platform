#!/usr/bin/env python3
"""
langflow-openai-adapter — a tiny dependency-free HTTP shim that exposes
LangFlow flows as an OpenAI-compatible /v1/chat/completions endpoint.

Why this exists
---------------
LangFlow 1.10.1's OpenAI surface is too partial for LibreChat's plain
custom-endpoint client:

  * it only implements the Responses API (`POST /api/v1/responses`), not
    `/chat/completions`;
  * that Responses endpoint requires `input` as a *string*, while LibreChat
    sends the OpenAI Responses `input` as an *array* -> 422 "Input should be
    a valid string";
  * in auto-login mode it rejects `Authorization: Bearer` (LibreChat's only
    way to send a key) -> 401/403.

This adapter sidesteps all three: LibreChat talks plain chat-completions to
us, we pull the user text out of the `messages` array and call LangFlow's
native `POST /api/v1/run/<flow_id>` (string `input_value`), then wrap the
flow output back into an OpenAI chat-completions response. Internal-only:
bound to the `backend` network; the public flow.<domain> path stays gated
by oauth2-proxy.

Stdlib only — no pip, no image build. Bind-mounted into python:3.12-slim.
"""

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

LANGFLOW_BASE_URL = os.environ.get("LANGFLOW_BASE_URL", "http://langflow:7860").rstrip("/")

# Approval reference shape (contracts: ar-approval-<uuid>). Used to detect a
# human's approval/reject reply so the adapter forwards it unchanged and the
# supervisor resumes its paused LangGraph checkpoint via session_id (§19/§11).
APPROVAL_REF_RE = re.compile(
    r"ar-approval-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
# Optional static model list for GET /v1/models (comma-separated flow ids).
# When empty, /v1/models returns an empty list — LibreChat's `models.default`
# in librechat.yaml is the authoritative list, so this is only needed if you
# set `fetch: true` on the endpoint.
FLOW_IDS = [x.strip() for x in os.environ.get("LANGFLOW_FLOW_IDS", "").split(",") if x.strip()]
PORT = int(os.environ.get("ADAPTER_PORT", "8080"))
RUN_TIMEOUT = int(os.environ.get("ADAPTER_RUN_TIMEOUT", "300"))


def extract_user_text(messages):
    """Concatenate the text of every user message.

    LibreChat may send `content` as a plain string or as an OpenAI
    content-parts array (`[{"type":"text","text":"..."}]`). Handle both.
    """
    texts = []
    for msg in messages or []:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") in ("text", "input_text"):
                    texts.append(part.get("text", ""))
                elif isinstance(part, str):
                    texts.append(part)
    return "\n".join(t for t in texts if t)


def extract_responses_input(payload):
    """Extract text from an OpenAI Responses API `input` field.

    `input` may be a plain string or an array of message items, each either a
    string or an object like {"role":"user","content":"..."} or
    {"role":"user","content":[{"type":"input_text","text":"..."}]}. The array
    form is exactly what LangFlow's own /v1/responses rejects with 422
    ("Input should be a valid string"); we accept it and reduce it to a string.
    """
    inp = payload.get("input")
    if isinstance(inp, str):
        return inp
    texts = []
    if isinstance(inp, list):
        for item in inp:
            if isinstance(item, str):
                texts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") in ("input_text", "output_text", "text"):
                        texts.append(part.get("text", ""))
                    elif isinstance(part, str):
                        texts.append(part)
    return "\n".join(t for t in texts if t)


def _decode_data_url(url):
    """Decode a `data:<mime>;base64,<payload>` URL into (bytes, mime) or None.

    Returns None for non-data URLs (we do NOT fetch remote URLs — best-effort,
    no SSRF surface) and for malformed data URLs.
    """
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    try:
        header, payload = url.split(",", 1)
    except ValueError:
        return None
    mime = header[5:].split(";")[0] or "application/octet-stream"
    if "base64" not in header:
        return None
    try:
        # validate=True so non-base64 chars (garbage/truncation) raise instead of
        # silently producing wrong bytes we'd then upload to LangFlow.
        return base64.b64decode(payload, validate=True), mime
    except Exception:  # noqa: BLE001 - malformed/truncated base64
        return None


def extract_files(messages):
    """Collect uploaded files from an OpenAI/LibreChat messages array.

    Returns a list of `(filename, bytes, content_type)` tuples. Handles, in
    priority order:

      * LibreChat `attachments` on a user message (each `{url|filepath,
        filename, contentType|mime}` — `url` may be a data URL).
      * OpenAI content-part `image_url` / `input_image` / `output_image`
        carrying a data URL.
      * OpenAI content-part `input_file` / `file` with `file_data` (data URL)
        or raw bytes.

    Remote http(s) URLs are deliberately NOT fetched (no SSRF surface; the
    supervisor only needs files the client already has). Best-effort: any
    part we can't decode is skipped, never raised.
    """
    out = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        # LibreChat attachments (often on the user message, independent of
        # content-part shape).
        for att in msg.get("attachments") or []:
            if not isinstance(att, dict):
                continue
            fname = att.get("filename") or att.get("name") or ""
            mime = att.get("contentType") or att.get("mime") or att.get("mimeType") or ""
            url = att.get("url") or att.get("filepath") or ""
            decoded = _decode_data_url(url)
            if decoded is not None:
                data, dmime = decoded
                out.append((fname or "attachment", data, mime or dmime))
            continue
        # OpenAI content-parts array.
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            fname = part.get("filename") or part.get("name") or ""
            url = None
            if ptype in ("image_url", "input_image", "output_image"):
                iu = part.get("image_url")
                url = iu.get("url") if isinstance(iu, dict) else iu
            elif ptype in ("input_file", "file"):
                fd = part.get("file_data")
                url = fd.get("url") if isinstance(fd, dict) else fd
                if url is None and isinstance(part.get("file_data"), (bytes, bytearray)):
                    out.append((fname or "file", bytes(part["file_data"]),
                                part.get("content_type") or "application/octet-stream"))
                    continue
            if url:
                decoded = _decode_data_url(url)
                if decoded is not None:
                    data, dmime = decoded
                    if ptype and "image" in ptype and not fname:
                        fname = "image" + _ext_for_mime(dmime)
                    out.append((fname or "upload", data, dmime))
    return out


def _ext_for_mime(mime):
    """Best-effort file extension for a mime type (used for image filenames)."""
    return {
        "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
        "image/webp": ".webp", "image/svg+xml": ".svg", "application/pdf": ".pdf",
        "text/csv": ".csv", "text/plain": ".txt",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    }.get(mime, "")


def upload_files(flow_id, files):
    """Upload files to LangFlow's file API, returning their `file_path` strings.

    POSTs each file as multipart/form-data (`file` field) to
    `/api/v1/files/upload/{flow_id}` and parses the `UploadFileResponse`
    `{flow_id, file_path}`. Best-effort: on any failure we log (stderr, no PII
    — only the filename and the HTTP status, never file contents) and continue,
    so a file hiccup never blocks the chat. Returns [] when nothing uploaded.
    """
    if not files:
        return []
    paths = []
    for fname, data, ctype in files:
        boundary = "----adapter-boundary-" + uuid.uuid4().hex
        safe_name = (fname or "upload").replace('"', "")
        disposition = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{safe_name}"\r\nContent-Type: {ctype or "application/octet-stream"}'
            f'\r\n\r\n'
        ).encode("utf-8")
        body = disposition + data + f"\r\n--{boundary}--\r\n".encode("utf-8")
        req = urllib.request.Request(
            f"{LANGFLOW_BASE_URL}/api/v1/files/upload/{flow_id}",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=RUN_TIMEOUT) as resp:
                up = json.loads(resp.read().decode("utf-8") or "{}")
            fp = up.get("file_path") if isinstance(up, dict) else None
            if isinstance(fp, str) and fp:
                paths.append(fp)
            else:
                sys.stderr.write(f"[adapter] file upload no file_path for {safe_name}\n")
        except urllib.error.HTTPError as exc:
            sys.stderr.write(f"[adapter] file upload {safe_name} HTTP {exc.code}\n")
        except Exception as exc:  # noqa: BLE001 - never block on a file
            sys.stderr.write(f"[adapter] file upload {safe_name} failed: {exc}\n")
    sys.stderr.flush()
    return paths


def parse_envelope(text):
    """Best-effort parse of a §14 envelope from a flow output string.

    Returns the envelope dict, or None when `text` is not a JSON object (e.g.
    a plain human-readable answer). Used to surface pending_approval to the
    user via a friendly prompt + `x_cosmic_approval` metadata.
    """
    if not isinstance(text, str) or not text:
        return None
    try:
        obj = json.loads(text)
    except (TypeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def detect_approval_reply(user_text):
    """Return the first `ar-approval-<uuid>` in `user_text`, or None.

    When a human replies with an approval_ref, the adapter forwards the text
    unchanged (the supervisor's resume path detects the ref and resumes the
    paused checkpoint via `session_id`). This helper is used for debug logging
    and is the symmetric counterpart of the supervisor's approval-ref emitter.
    """
    if not isinstance(user_text, str) or not user_text:
        return None
    match = APPROVAL_REF_RE.search(user_text)
    return match.group(0) if match else None


def render_approval(envelope):
    """Render a pending_approval envelope for a human reader + metadata.

    Returns `(prompt_text, x_cosmic_approval)` where `prompt_text` is a
    friendly instruction (reply `approve <ref>` / `reject <ref>`) and
    `x_cosmic_approval` is the structured `{status, approval_ref, action,
    checkpoint_id}` object a future LibreChat plugin can render as a button.
    Returns `(None, None)` when `envelope` is not a pending_approval envelope.
    """
    if not isinstance(envelope, dict) or envelope.get("status") != "pending_approval":
        return None, None
    ref = envelope.get("approval_ref", "") or ""
    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
    action = data.get("action") or envelope.get("intent") or "this action"
    checkpoint = envelope.get("checkpoint_id") or data.get("checkpoint_id") or ""
    tier = data.get("tier", "approval")
    prompt = (
        f"⏳ Awaiting approval for {action} (tier: {tier})."
        + (f"\nReference: {ref}" if ref else "")
        + "\nReply `approve " + ref + "` to proceed, or `reject " + ref + "` to cancel."
    )
    x_cosmic_approval = {
        "status": "pending_approval",
        "approval_ref": ref,
        "action": action,
        "checkpoint_id": checkpoint,
    }
    return prompt, x_cosmic_approval


def run_flow(flow_id, text, session_id=None, files=None):
    """POST a string input to LangFlow's native run endpoint and return the JSON.

    `session_id` (when present) is forwarded so LangFlow persists the flow's
    memory/checkpoints under that id across turns. When absent, LangFlow mints a
    fresh session per call (no cross-turn memory). `files` (when present) is a
    list of LangFlow file paths (as returned by `upload_files`); LangFlow's
    `_validate_public_files` requires each to be `{flow_id}/{basename}`, which
    the upload endpoint produces. The supervisor's File node receives them.
    """
    # `input_type: "any"` (not "text") so the simplified /run API injects
    # `input_value` into the flow's input node regardless of its type.
    # LangFlow's run API filters input vertices by type: "text" only reaches
    # TextInput nodes, "chat" only ChatInput. The AR flows (supervisor, intake)
    # use ChatInput, so "text" silently dropped the user's message and the
    # supervisor classified the ChatInput's default ("Hello") → AR_UNCERTAIN on
    # every turn. "any" reaches both ChatInput and TextInput (robust for the
    # generic bridge) and is the value that makes chat messages arrive.
    body = {"input_value": text, "input_type": "any", "output_type": "text"}
    if session_id:
        body["session_id"] = session_id
    if files:
        body["files"] = files
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{LANGFLOW_BASE_URL}/api/v1/run/{flow_id}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=RUN_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run_flow_stream(flow_id, text, session_id=None, files=None):
    """Stream a flow run from LangFlow, yielding (kind, text) tuples.

    LangFlow's streamed /run emits raw JSON-per-line (NOT `data:`-prefixed SSE),
    e.g. `{"event":"add_message","data":{"data":{"text":"..."}}}` and a terminal
    `{"event":"end","data":{"result":{...}}}`. We yield:

      * ("message", chunk)  for each add_message content event
      * ("end", final_text) once, when the run completes

    add_message events carry CUMULATIVE message snapshots (the full message so
    far, not a delta), so the caller must diff against what it has already
    forwarded to avoid echoing the same text twice. `files` is forwarded the
    same way as in `run_flow`.
    """
    body = {"input_value": text, "input_type": "any", "output_type": "text", "stream": True}
    if session_id:
        body["session_id"] = session_id
    if files:
        body["files"] = files
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{LANGFLOW_BASE_URL}/api/v1/run/{flow_id}?stream=true",
        data=data,
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=RUN_TIMEOUT) as resp:
        for raw in resp:
            if raw is None:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except Exception:  # noqa: BLE001 - skip non-JSON keepalive lines
                continue
            kind = evt.get("event")
            if kind == "add_message":
                txt = (evt.get("data") or {}).get("data", {}).get("text", "")
                if txt:
                    yield ("message", txt)
            elif kind == "end":
                result = (evt.get("data") or {}).get("result") or {}
                yield ("end", extract_output_text(result))
                return


def extract_conversation_id(payload, headers):
    """Best-effort lookup of LibreChat's conversationId in the upstream request.

    LibreChat's plain custom OpenAI-compatible endpoint normally sends only the
    OpenAI chat-completions schema (no conversationId). If a future LibreChat
    build — or a custom header/`addParams`/`metadata` injection — surfaces it,
    accept any of these locations. Returns None when not present.
    """
    cid = payload.get("conversationId") or payload.get("conversation_id")
    if not cid:
        meta = payload.get("metadata")
        if isinstance(meta, dict):
            cid = meta.get("conversationId") or meta.get("conversation_id")
    if not cid:
        cid = (
            headers.get("X-Conversation-Id")
            or headers.get("X-LibreChat-Conversation-Id")
            or headers.get("x-conversation-id")
        )
    return cid


def extract_output_text(resp):
    """Best-effort extraction of the flow's text output from a /run response.

    LangFlow wraps results as outputs[].outputs[].results.message.data.text
    (with `text_key` naming the field). Be defensive: versions/layouts vary.
    """
    chunks = []
    for top in resp.get("outputs", []) or []:
        for out in top.get("outputs", []) or []:
            results = out.get("results") or {}
            message = results.get("message") or {}
            data = message.get("data") if isinstance(message, dict) else None
            if isinstance(data, dict) and data.get("text"):
                chunks.append(data["text"])
            elif isinstance(message, dict) and message.get("text"):
                chunks.append(message["text"])
            artifacts = out.get("artifacts") or {}
            if isinstance(artifacts, dict) and artifacts.get("string"):
                chunks.append(artifacts["string"])
    if chunks:
        return "\n".join(chunks)
    # Last resort: surface the raw payload (truncated) so the user sees *something*.
    return json.dumps(resp)[:2000]


class Handler(BaseHTTPRequestHandler):
    server_version = "langflow-openai-adapter/1.0"
    # HTTP/1.1 so non-stream responses (which carry Content-Length) can keep-alive
    # and clients parse them as modern responses. The streaming path forces
    # Connection: close (see do_POST) so the SSE body is close-delimited and the
    # response terminates cleanly after [DONE] — otherwise the socket stays open
    # and LibreChat hangs in its "generating" state waiting for the response end.
    protocol_version = "HTTP/1.1"

    # ---- helpers ----------------------------------------------------------
    def _send(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_error(self, code, message):
        self._send(code, json.dumps({"error": {"message": message, "type": "adapter_error"}}).encode())

    # ---- routes -----------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/health", "/healthz", "/"):
            return self._send(200, b'{"status":"ok"}')
        if path == "/v1/models":
            data = [
                {"id": fid, "object": "model", "created": 0, "owned_by": "langflow"}
                for fid in FLOW_IDS
            ]
            return self._send(200, json.dumps({"object": "list", "data": data}).encode())
        return self._send_error(404, f"not found: {path}")

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path not in ("/v1/chat/completions", "/chat/completions", "/v1/responses", "/responses"):
            return self._send_error(404, f"not found: {path}")

        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception as exc:  # noqa: BLE001 - report any parse failure
            return self._send_error(400, f"invalid JSON body: {exc}")

        if os.environ.get("ADAPTER_DEBUG") == "1":
            safe_headers = {
                k: v for k, v in self.headers.items()
                if k.lower() not in ("authorization", "cookie")
            }
            sys.stderr.write(
                "[adapter] POST %s headers=%s body_keys=%s\n"
                % (
                    path,
                    json.dumps(safe_headers),
                    json.dumps(list(payload.keys())),
                )
            )
            cid_dbg = extract_conversation_id(payload, self.headers)
            sys.stderr.write(
                "[adapter] conversationId resolved=%r (body=%r metadata=%r)\n"
                % (
                    cid_dbg,
                    payload.get("conversationId"),
                    payload.get("metadata"),
                )
            )
            sys.stderr.flush()

        # LibreChat Agents use the OpenAI Responses API (POST /v1/responses);
        # plain custom endpoints use chat-completions. Dispatch accordingly.
        if path in ("/v1/responses", "/responses"):
            return self._responses(payload)

        # The model id IS the flow id (or flow name) — passed straight through.
        flow_id = payload.get("model") or (FLOW_IDS[0] if FLOW_IDS else "")
        if not flow_id:
            return self._send_error(400, "model (flow id) is required")

        user_text = extract_user_text(payload.get("messages", []))
        # Forward LibreChat's conversationId as LangFlow's session_id so the flow
        # retains memory across the same LibreChat conversation thread. None when
        # LibreChat doesn't surface it (LangFlow then mints a per-call session).
        conversation_id = extract_conversation_id(payload, self.headers)
        # Accept uploaded files: extract from the messages, upload to LangFlow's
        # file API, and forward the resulting paths into the /run body. Best-effort
        # — a file hiccup never blocks the chat (text-only fallback).
        files = upload_files(flow_id, extract_files(payload.get("messages", [])))
        if os.environ.get("ADAPTER_DEBUG") == "1":
            sys.stderr.write(
                "[adapter] files=%r conversationId=%r approval_reply=%r\n"
                % (files, conversation_id, detect_approval_reply(user_text))
            )
            sys.stderr.flush()

        # Streaming path: forward LangFlow's streamed chunks to LibreChat as
        # OpenAI chat.completion.chunk SSE deltas AS THEY ARRIVE, so LibreChat
        # shows the "generating" state immediately and renders tokens the moment
        # the flow emits them (instead of blocking ~8s on the full /run).
        if payload.get("stream"):
            return self._stream_completion(flow_id, user_text, conversation_id, files)

        # Non-streaming path: block on the full /run, return one JSON object.
        try:
            lf_resp = run_flow(flow_id, user_text, conversation_id, files)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            return self._send(exc.code, body or json.dumps({"error": {"message": "langflow error"}}).encode())
        except Exception as exc:  # noqa: BLE001 - connection/timeout etc.
            return self._send_error(502, f"langflow unreachable: {exc}")

        content = extract_output_text(lf_resp)
        # Surface a pending_approval envelope as a human-readable prompt + the
        # structured `x_cosmic_approval` metadata (a future LibreChat plugin can
        # render an approve/reject button from it). §14/§19.
        prompt, x_cosmic_approval = render_approval(parse_envelope(content))
        if prompt is not None:
            content = prompt
        cid = f"chatcmpl-{int(time.time() * 1000)}"
        created = int(time.time())
        body = {
            "id": cid,
            "object": "chat.completion",
            "created": created,
            "model": flow_id,
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        if x_cosmic_approval is not None:
            body["x_cosmic_approval"] = x_cosmic_approval
        return self._send(200, json.dumps(body).encode("utf-8"))

    def _stream_completion(self, flow_id, user_text, conversation_id, files=None):
        """Stream LangFlow's /run output to LibreChat as OpenAI SSE chunks.

        LangFlow emits raw JSON-per-line add_message events carrying CUMULATIVE
        message snapshots, so we diff against what we've already forwarded and
        emit only the new characters as chat.completion.chunk deltas. The body is
        close-delimited (Connection: close) so the response ends cleanly after
        [DONE] — see the protocol_version note on the Handler class. `files`
        (uploaded-file paths) is forwarded into the /run body.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()

        cid = f"chatcmpl-{int(time.time() * 1000)}"
        created = int(time.time())

        def write_chunk(delta, finish=None):
            payload_ = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": flow_id,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            self.wfile.write(("data: " + json.dumps(payload_) + "\n\n").encode("utf-8"))
            self.wfile.flush()

        def finish_stream(extra_content=None):
            if extra_content:
                write_chunk({"content": extra_content})
            write_chunk({}, "stop")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        sent_text = ""
        try:
            write_chunk({"role": "assistant", "content": ""})
            for kind, text in run_flow_stream(flow_id, user_text, conversation_id, files):
                if not text:
                    continue
                if kind == "end":
                    # If the flow never streamed answer text (only tool calls),
                    # emit the final result so LibreChat isn't left empty.
                    if not sent_text:
                        sent_text = text
                        write_chunk({"content": text})
                    continue
                # add_message: cumulative snapshot -> forward only the new suffix.
                if text.startswith(sent_text):
                    delta = text[len(sent_text):]
                    sent_text = text
                elif sent_text.startswith(text):
                    continue  # older/shorter snapshot, nothing new
                else:
                    delta = text
                    sent_text += text
                if delta:
                    write_chunk({"content": delta})
            finish_stream()
        except urllib.error.HTTPError as exc:
            try:
                err = exc.read().decode("utf-8", errors="replace")[:500] or str(exc)
            except Exception:  # noqa: BLE001
                err = str(exc)
            try:
                finish_stream(f"[adapter] langflow error {exc.code}: {err}")
            except (BrokenPipeError, ConnectionResetError):
                pass
        except (BrokenPipeError, ConnectionResetError):
            pass  # LibreChat disconnected mid-stream; nothing more to do
        except Exception as exc:  # noqa: BLE001 - don't leave the SSE stream dangling
            try:
                finish_stream(f"[adapter] error: {exc}")
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _responses(self, payload):
        """OpenAI Responses API (POST /v1/responses) — used by LibreChat Agents.

        Accepts `input` as a string OR an array of message items (the shape that
        LangFlow's own /v1/responses 422s on), reduces it to a string, runs the
        flow, and returns either an OpenAI Response object (non-stream) or the
        Responses SSE event sequence (stream) that the OpenAI SDK / langchainjs
        parse to produce the assistant message.
        """
        flow_id = payload.get("model") or (FLOW_IDS[0] if FLOW_IDS else "")
        if not flow_id:
            return self._send_error(400, "model (flow id) is required")
        input_text = extract_responses_input(payload)
        instructions = payload.get("instructions") or ""
        conversation_id = extract_conversation_id(payload, self.headers)
        full_input = (instructions + "\n\n" if instructions else "") + input_text
        # Accept uploaded files: the Responses API carries files as content parts
        # on the `input` items; reuse the chat-completions extractor (it walks
        # content parts the same way) on a synthesized messages list.
        input_items = payload.get("input")
        file_msgs = input_items if isinstance(input_items, list) else [
            {"role": "user", "content": input_items if isinstance(input_items, list) else []}
        ]
        files = upload_files(flow_id, extract_files(file_msgs))

        if payload.get("stream"):
            return self._stream_responses(flow_id, full_input, conversation_id, files)

        try:
            lf_resp = run_flow(flow_id, full_input, conversation_id, files)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            return self._send(exc.code, body or json.dumps({"error": {"message": "langflow error"}}).encode())
        except Exception as exc:  # noqa: BLE001 - connection/timeout etc.
            return self._send_error(502, f"langflow unreachable: {exc}")

        content = extract_output_text(lf_resp)
        # Surface a pending_approval envelope as a friendly prompt + structured
        # metadata under `response.metadata.x_cosmic_approval` (§14/§19).
        prompt, x_cosmic_approval = render_approval(parse_envelope(content))
        if prompt is not None:
            content = prompt
        rid = f"resp_{int(time.time() * 1000)}"
        mid = f"msg_{int(time.time() * 1000)}"
        resp = {
            "id": rid,
            "object": "response",
            "created_at": int(time.time()),
            "status": "completed",
            "model": flow_id,
            "output": [{
                "id": mid,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": content, "annotations": []}],
            }],
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }
        if x_cosmic_approval is not None:
            resp["metadata"] = {"x_cosmic_approval": x_cosmic_approval}
        return self._send(200, json.dumps(resp).encode("utf-8"))

    def _stream_responses(self, flow_id, full_input, conversation_id, files=None):
        """Emit the OpenAI Responses SSE event sequence for a streamed flow run.

        Event order: response.created -> response.in_progress ->
        response.output_item.added -> response.content_part.added ->
        response.output_text.delta (repeated) -> response.output_text.done ->
        response.output_item.done -> response.completed. The OpenAI SDK keys off
        `response.output_text.delta` for tokens and `response.completed` to
        finalize; omitting the terminal events makes the client hang.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()

        rid = f"resp_{int(time.time() * 1000)}"
        mid = f"msg_{int(time.time() * 1000)}"
        created_at = int(time.time())

        def sse(event, data):
            self.wfile.write(f"event: {event}\n".encode("utf-8"))
            self.wfile.write(("data: " + json.dumps(data) + "\n\n").encode("utf-8"))
            self.wfile.flush()

        def response_obj(status, output=None):
            return {
                "id": rid,
                "object": "response",
                "created_at": created_at,
                "status": status,
                "model": flow_id,
                "output": output or [],
            }

        sent_text = ""
        try:
            sse("response.created", {"type": "response.created", "response": response_obj("in_progress")})
            sse("response.in_progress", {"type": "response.in_progress", "response": response_obj("in_progress")})
            sse("response.output_item.added", {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"id": mid, "type": "message", "status": "in_progress", "role": "assistant", "content": []},
            })
            sse("response.content_part.added", {
                "type": "response.content_part.added",
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            })

            for kind, text in run_flow_stream(flow_id, full_input, conversation_id, files):
                if not text:
                    continue
                if kind == "end":
                    if not sent_text:
                        sent_text = text
                        sse("response.output_text.delta", {
                            "type": "response.output_text.delta",
                            "output_index": 0, "content_index": 0, "delta": text,
                        })
                    continue
                # add_message carries cumulative snapshots -> forward only new chars.
                if text.startswith(sent_text):
                    delta = text[len(sent_text):]
                    sent_text = text
                elif sent_text.startswith(text):
                    continue  # older/shorter snapshot, nothing new
                else:
                    delta = text
                    sent_text += text
                if delta:
                    sse("response.output_text.delta", {
                        "type": "response.output_text.delta",
                        "output_index": 0, "content_index": 0, "delta": delta,
                    })

            sse("response.output_text.done", {
                "type": "response.output_text.done",
                "output_index": 0, "content_index": 0, "text": sent_text,
            })
            done_item = {
                "id": mid, "type": "message", "status": "completed", "role": "assistant",
                "content": [{"type": "output_text", "text": sent_text, "annotations": []}],
            }
            sse("response.output_item.done", {
                "type": "response.output_item.done", "output_index": 0, "item": done_item,
            })
            completed_resp = response_obj("completed", [done_item])
            completed_resp["usage"] = {
                "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            }
            sse("response.completed", {
                "type": "response.completed", "response": completed_resp,
            })
        except urllib.error.HTTPError as exc:
            try:
                err = exc.read().decode("utf-8", errors="replace")[:500] or str(exc)
                sse("response.failed", {
                    "type": "response.failed",
                    "response": response_obj("failed"),
                    "error": {"message": f"langflow error {exc.code}: {err}"},
                })
            except (BrokenPipeError, ConnectionResetError):
                pass
        except (BrokenPipeError, ConnectionResetError):
            pass  # client disconnected
        except Exception as exc:  # noqa: BLE001 - emit a failure event, don't dangle
            try:
                sse("response.failed", {
                    "type": "response.failed",
                    "response": response_obj("failed"),
                    "error": {"message": str(exc)},
                })
            except (BrokenPipeError, ConnectionResetError):
                pass

    # Quiet by default — flip ADAPTER_DEBUG=1 for access logging.
    def log_message(self, fmt, *args):
        if os.environ.get("ADAPTER_DEBUG") == "1":
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(
        f"langflow-openai-adapter listening on :{PORT} "
        f"(upstream={LANGFLOW_BASE_URL}, flows={FLOW_IDS})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()