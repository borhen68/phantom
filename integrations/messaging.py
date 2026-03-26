"""Telegram and WhatsApp webhook integration for PHANTOM."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
import sqlite3
import hmac
import hashlib
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import memory as mem
from core.settings import override_scope


DEFAULT_WHATSAPP_API_VERSION = "v23.0"
DEFAULT_REPLY_LIMIT = 3500
HELP_TEXT = (
    "What do you want PHANTOM to do? Send a concrete task in text. "
    "Example: audit this repository for bugs and propose fixes."
)
EMPTY_TEXT_HELP = (
    "What do you want PHANTOM to do? Send a concrete task in text. "
    "If you sent an image or file, add a caption describing the task."
)
GREETING_TOKENS = {"hi", "hello", "hey", "yo", "sup", "start"}


@dataclass(frozen=True)
class InboundMessage:
    platform: str
    message_id: str
    conversation_id: str
    sender_id: str
    text: str
    sender_name: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def _safe_fragment(value: str) -> str:
    chars = [char if char.isalnum() else "_" for char in str(value or "")]
    return "".join(chars).strip("_")[:80] or "conversation"


def messaging_scope(message: InboundMessage) -> str:
    return f"messaging::{message.platform}::{_safe_fragment(message.conversation_id)}"


def _first(mapping: Mapping[str, Any], key: str, default: str = "") -> str:
    value = mapping.get(key, default)
    if isinstance(value, list):
        return str(value[0]) if value else default
    return str(value)


def _extract_whatsapp_text(message: Mapping[str, Any]) -> str:
    message_type = str(message.get("type") or "").strip().lower()
    if message_type == "text":
        return str((message.get("text") or {}).get("body") or "").strip()
    if message_type == "button":
        return str((message.get("button") or {}).get("text") or "").strip()
    if message_type == "interactive":
        interactive = message.get("interactive") or {}
        button_reply = interactive.get("button_reply") or {}
        list_reply = interactive.get("list_reply") or {}
        return str(button_reply.get("title") or list_reply.get("title") or "").strip()
    if message_type == "image":
        return str((message.get("image") or {}).get("caption") or "").strip()
    return ""


def parse_telegram_update(payload: Mapping[str, Any]) -> InboundMessage | None:
    message = payload.get("message") or payload.get("edited_message")
    if not isinstance(message, Mapping):
        return None
    text = str(message.get("text") or message.get("caption") or "").strip()

    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id = str(chat.get("id") or "").strip()
    message_id = str(message.get("message_id") or payload.get("update_id") or "").strip()
    sender_id = str(sender.get("id") or chat_id).strip()
    if not chat_id or not message_id:
        return None

    sender_name = (
        str(sender.get("username") or "").strip()
        or " ".join(part for part in (sender.get("first_name"), sender.get("last_name")) if part).strip()
        or str(chat.get("title") or "").strip()
        or sender_id
    )
    return InboundMessage(
        platform="telegram",
        message_id=message_id,
        conversation_id=chat_id,
        sender_id=sender_id,
        sender_name=sender_name,
        text=text,
        raw=dict(payload),
    )


def parse_whatsapp_payload(payload: Mapping[str, Any]) -> list[InboundMessage]:
    inbound: list[InboundMessage] = []
    for entry in payload.get("entry", []) or []:
        for change in (entry.get("changes", []) or []):
            value = change.get("value") or {}
            contacts = {
                str(contact.get("wa_id") or ""): contact
                for contact in (value.get("contacts", []) or [])
                if contact.get("wa_id")
            }
            for message in (value.get("messages", []) or []):
                text = _extract_whatsapp_text(message)
                sender_id = str(message.get("from") or "").strip()
                message_id = str(message.get("id") or "").strip()
                if not sender_id or not message_id:
                    continue
                contact = contacts.get(sender_id) or {}
                profile = contact.get("profile") or {}
                inbound.append(
                    InboundMessage(
                        platform="whatsapp",
                        message_id=message_id,
                        conversation_id=sender_id,
                        sender_id=sender_id,
                        sender_name=str(profile.get("name") or sender_id),
                        text=text,
                        raw=dict(payload),
                    )
                )
    return inbound


def validate_telegram_secret(headers: Mapping[str, str], expected_secret: str | None) -> bool:
    if not expected_secret:
        return True
    return headers.get("X-Telegram-Bot-Api-Secret-Token", "") == expected_secret


def verify_whatsapp_handshake(params: Mapping[str, Any], expected_token: str | None) -> tuple[int, str]:
    mode = _first(params, "hub.mode")
    challenge = _first(params, "hub.challenge")
    token = _first(params, "hub.verify_token")
    if mode == "subscribe" and challenge and (not expected_token or token == expected_token):
        return 200, challenge
    return 403, "forbidden"


def verify_whatsapp_signature(body: bytes, header_sig: str | None, secret: str | None) -> bool:
    if not secret:
        return True
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, str(header_sig or ""))


def _http_json(
    method: str,
    url: str,
    payload: dict[str, Any],
    *,
    headers: Mapping[str, str] | None = None,
    timeout: int = 20,
) -> dict[str, Any]:
    request_headers = {"Content-Type": "application/json"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=request_headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
        body = response.read().decode("utf-8")
    return json.loads(body) if body else {}


def send_telegram_message(bot_token: str, chat_id: str, text: str) -> dict[str, Any]:
    if not bot_token:
        raise EnvironmentError("TELEGRAM_BOT_TOKEN is not configured.")
    return _http_json(
        "POST",
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        {"chat_id": chat_id, "text": text},
    )


def set_telegram_webhook(bot_token: str, url: str, secret_token: str | None = None) -> dict[str, Any]:
    payload = {"url": url}
    if secret_token:
        payload["secret_token"] = secret_token
    return _http_json(
        "POST",
        f"https://api.telegram.org/bot{bot_token}/setWebhook",
        payload,
    )


def send_whatsapp_message(
    access_token: str,
    phone_number_id: str,
    to: str,
    text: str,
    *,
    api_version: str = DEFAULT_WHATSAPP_API_VERSION,
) -> dict[str, Any]:
    if not access_token:
        raise EnvironmentError("WHATSAPP_ACCESS_TOKEN is not configured.")
    if not phone_number_id:
        raise EnvironmentError("WHATSAPP_PHONE_NUMBER_ID is not configured.")
    return _http_json(
        "POST",
        f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages",
        {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text},
        },
        headers={"Authorization": f"Bearer {access_token}"},
    )


class MessagingService:
    """Async inbound message runner that executes PHANTOM goals per conversation."""

    def __init__(
        self,
        *,
        run_goal: Callable[..., dict[str, Any]] | None = None,
        telegram_sender: Callable[[str, str], Any] | None = None,
        whatsapp_sender: Callable[[str, str], Any] | None = None,
        on_event=None,
        max_workers: int = 2,
    ):
        mem.init()
        self.run_goal = run_goal or self._default_run_goal
        self.telegram_sender = telegram_sender or self._default_telegram_sender
        self.whatsapp_sender = whatsapp_sender or self._default_whatsapp_sender
        self.on_event = on_event
        self.max_workers = max(1, int(max_workers))
        self.executor = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="phantom-msg")
        self._seen_lock = threading.Lock()
        self._seen_messages: dict[str, float] = {}

    def _default_run_goal(self, **kwargs) -> dict[str, Any]:
        from core.orchestrator import run

        return run(**kwargs)

    def _default_telegram_sender(self, conversation_id: str, text: str) -> Any:
        return send_telegram_message(os.environ.get("TELEGRAM_BOT_TOKEN", ""), conversation_id, text)

    def _default_whatsapp_sender(self, recipient_id: str, text: str) -> Any:
        return send_whatsapp_message(
            os.environ.get("WHATSAPP_ACCESS_TOKEN", ""),
            os.environ.get("WHATSAPP_PHONE_NUMBER_ID", ""),
            recipient_id,
            text,
            api_version=os.environ.get("WHATSAPP_API_VERSION", DEFAULT_WHATSAPP_API_VERSION),
        )

    def shutdown(self, wait: bool = True) -> None:
        self.executor.shutdown(wait=wait, cancel_futures=False)

    def _message_key(self, message: InboundMessage) -> str:
        return f"{message.platform}:{message.message_id}"

    def _remember_message(self, message: InboundMessage) -> bool:
        key = self._message_key(message)
        now = time.time()
        with self._seen_lock:
            if key in self._seen_messages:
                return False
            if self._seen_in_db(key, now):
                return False
            self._seen_messages[key] = now
            self._prune_seen_cache(now)
            return True

    def _seen_in_db(self, key: str, now: float) -> bool:
        database = mem.db_path()
        database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database, timeout=10.0) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS msg_dedupe (key TEXT PRIMARY KEY, seen_at REAL NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_msg_dedupe_seen_at ON msg_dedupe(seen_at)"
            )
            connection.execute(
                "DELETE FROM msg_dedupe WHERE seen_at < ?",
                (now - 86400,),
            )
            try:
                connection.execute(
                    "INSERT INTO msg_dedupe (key, seen_at) VALUES (?, ?)",
                    (key, now),
                )
                connection.commit()
                return False
            except sqlite3.IntegrityError:
                return True

    def _prune_seen_cache(self, now: float) -> None:
        if len(self._seen_messages) <= 4096:
            return
        cutoff = now - 86400
        stale = [item for item, ts in list(self._seen_messages.items())[:1024] if ts < cutoff]
        for item in stale:
            self._seen_messages.pop(item, None)
        while len(self._seen_messages) > 4096:
            self._seen_messages.pop(next(iter(self._seen_messages)))

    def submit(self, message: InboundMessage) -> bool:
        if not self._remember_message(message):
            return False
        self.executor.submit(self.process_message, message)
        return True

    def process_message(self, message: InboundMessage) -> None:
        try:
            reply_text = self._reply_for_message(message)
        except Exception as exc:  # pragma: no cover - defensive background path
            reply_text = f"PHANTOM could not complete that request: {exc}"
        try:
            self._send_reply(message, reply_text)
        except Exception as exc:  # pragma: no cover - defensive background path
            print(
                f"[phantom-messaging] failed to send {message.platform} reply for {message.message_id}: {exc}",
                file=sys.stderr,
            )

    def _reply_for_message(self, message: InboundMessage) -> str:
        text = (message.text or "").strip()
        if not text:
            return EMPTY_TEXT_HELP
        normalized = text.lower().strip()
        if normalized in {"/start", "/help", "help"}:
            return HELP_TEXT
        if normalized in GREETING_TOKENS:
            return HELP_TEXT

        with override_scope(messaging_scope(message)):
            result = self.run_goal(goal=text, on_event=self.on_event, parallel=True)

        summary = str(result.get("summary") or "").strip()
        outcome = str(result.get("outcome") or "partial").strip().lower()
        if not summary:
            summary = f"Run finished with outcome: {outcome or 'partial'}."
        if outcome in {"failure", "partial"}:
            summary = f"[{outcome}] {summary}"
        return self._trim_reply(summary)

    def _trim_reply(self, text: str) -> str:
        limit = max(200, int(os.environ.get("PHANTOM_MESSAGING_REPLY_LIMIT", DEFAULT_REPLY_LIMIT)))
        cleaned = str(text or "").strip() or "PHANTOM completed the run."
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: limit - 3].rstrip() + "..."

    def _send_reply(self, message: InboundMessage, text: str) -> None:
        if message.platform == "telegram":
            self.telegram_sender(message.conversation_id, text)
            return
        if message.platform == "whatsapp":
            self.whatsapp_sender(message.sender_id, text)
            return
        raise ValueError(f"Unsupported messaging platform: {message.platform}")


class MessagingServer:
    """Thin wrapper around a threaded webhook server."""

    def __init__(self, httpd: ThreadingHTTPServer, service: MessagingService):
        self.httpd = httpd
        self.service = service

    @property
    def address(self) -> tuple[str, int]:
        host, port = self.httpd.server_address[:2]
        return str(host), int(port)

    def serve_forever(self) -> None:
        try:
            self.httpd.serve_forever()
        finally:
            self.service.shutdown(wait=False)

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.service.shutdown(wait=True)


def create_messaging_server(
    *,
    host: str = "0.0.0.0",
    port: int = 8080,
    service: MessagingService | None = None,
) -> MessagingServer:
    service = service or MessagingService(
        max_workers=max(1, int(os.environ.get("PHANTOM_MESSAGING_MAX_WORKERS", "2")))
    )

    class Handler(BaseHTTPRequestHandler):
        server_version = "PHANTOMMessaging/0.1"

        def log_message(self, fmt: str, *args) -> None:  # pragma: no cover - keep CLI quiet
            return

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, status: int, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _read_payload(self) -> dict[str, Any]:
            raw = self._read_raw_body()
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))

        def _read_raw_body(self) -> bytes:
            length = int(self.headers.get("Content-Length", "0") or 0)
            return self.rfile.read(length) if length else b"{}"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/healthz":
                self._send_json(200, {"ok": True, "service": "phantom-messaging"})
                return
            if parsed.path == "/whatsapp/webhook":
                status, body = verify_whatsapp_handshake(
                    urllib.parse.parse_qs(parsed.query),
                    os.environ.get("WHATSAPP_VERIFY_TOKEN"),
                )
                self._send_text(status, body)
                return
            self._send_json(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/telegram/webhook":
                try:
                    payload = self._read_payload()
                except json.JSONDecodeError:
                    self._send_json(400, {"error": "invalid_json"})
                    return
                if not validate_telegram_secret(
                    self.headers,
                    os.environ.get("TELEGRAM_WEBHOOK_SECRET_TOKEN"),
                ):
                    self._send_json(403, {"error": "forbidden"})
                    return
                message = parse_telegram_update(payload)
                if message is None:
                    self._send_json(200, {"accepted": 0, "received": 0})
                    return
                accepted = service.submit(message)
                self._send_json(200, {"accepted": int(accepted), "received": 1, "duplicate": not accepted})
                return

            if parsed.path == "/whatsapp/webhook":
                raw = self._read_raw_body()
                if not verify_whatsapp_signature(
                    raw,
                    self.headers.get("X-Hub-Signature-256"),
                    os.environ.get("WHATSAPP_APP_SECRET"),
                ):
                    self._send_json(403, {"error": "forbidden"})
                    return
                try:
                    payload = json.loads(raw.decode("utf-8")) if raw else {}
                except json.JSONDecodeError:
                    self._send_json(400, {"error": "invalid_json"})
                    return
                inbound = parse_whatsapp_payload(payload)
                accepted = sum(1 for message in inbound if service.submit(message))
                self._send_json(200, {"accepted": accepted, "received": len(inbound)})
                return

            self._send_json(404, {"error": "not_found"})

    httpd = ThreadingHTTPServer((host, port), Handler)
    return MessagingServer(httpd, service)
