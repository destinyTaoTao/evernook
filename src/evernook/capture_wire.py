"""Map ordinary host capture and stored evidence between wire and domain values."""
from __future__ import annotations

import base64
from dataclasses import asdict
from typing import Any

from .models import AttachmentInfo, AttachmentInput, Capture, CaptureReceipt, Event, MemoryFailure, Receipt, Source, StoredMessage


def encode_capture(capture: Capture) -> dict[str, Any]:
    attachments = []
    for item in capture.attachments:
        wire = asdict(item)
        chunks = wire.pop("chunks")
        wire["chunks_base64"] = None if chunks is None else [base64.b64encode(part).decode("ascii") for part in chunks]
        attachments.append(wire)
    return {"event": asdict(capture.event), "kind": capture.kind, "input_id": capture.input_id,
            "tool_name": capture.tool_name, "attachments": attachments, "provided": capture.provided,
            "text_chunks": capture.text_chunks}


def capture_receipt(value: dict[str, Any]) -> CaptureReceipt:
    if not isinstance(value["attachments"], list) or not isinstance(value["gaps"], list):
        raise ValueError("invalid_capture_receipt")
    return CaptureReceipt(**{**value, "message": Receipt(**value["message"]),
                          "attachments": tuple(AttachmentInfo(**item) for item in value["attachments"]),
                          "gaps": tuple(value["gaps"])})


def stored_message(event: dict[str, Any], receipt: dict[str, Any]) -> StoredMessage:
    return StoredMessage(Event(**{**event, "source": Source(**event["source"])}), Receipt(**receipt))


def decode_capture(value: dict[str, Any], *, max_attachment_chunks: int | None = None) -> Capture:
    try:
        fields = value.copy()
        event = fields.pop("event").copy()
        if not isinstance(event, dict) or not isinstance(event.get("source"), dict):
            raise MemoryFailure("invalid_arguments")
        event["source"] = Source(**event["source"])
        raw = fields.pop("attachments", [])
        if not isinstance(raw, list) or len(raw) > 16:
            raise MemoryFailure("invalid_capture")
        attachments = []
        for item in raw:
            if not isinstance(item, dict) or "chunks" in item:
                raise MemoryFailure("invalid_attachment")
            attachment = dict(item)
            encoded = attachment.pop("chunks_base64", None)
            chunks = None
            if encoded is not None:
                if (not isinstance(encoded, list) or (max_attachment_chunks is not None and len(encoded) > max_attachment_chunks)
                        or any(not isinstance(part, str) for part in encoded)):
                    raise MemoryFailure("invalid_attachment")
                chunks = tuple(base64.b64decode(part, validate=True) for part in encoded)
            attachments.append(AttachmentInput(chunks=chunks, **attachment))
        text = fields.pop("text_chunks", None)
        if text is not None and (not isinstance(text, list) or len(text) > 4096 or any(not isinstance(part, str) for part in text)):
            raise MemoryFailure("invalid_capture")
        return Capture(event=Event(**event), attachments=tuple(attachments),
                       text_chunks=None if text is None else tuple(text), **fields)
    except (AttributeError, TypeError, KeyError, ValueError):
        raise MemoryFailure("invalid_arguments") from None
