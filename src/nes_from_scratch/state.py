"""Portable, versioned, non-pickle save-state encoding."""

from __future__ import annotations

import base64
import json
import os
import tempfile
import zlib
from pathlib import Path
from typing import Any


MAGIC = b"FNESSTATE\x00"
FORMAT_VERSION = 1


class StateError(ValueError):
    pass


def _encode_value(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__bytes__": base64.b85encode(bytes(value)).decode("ascii")}
    if isinstance(value, dict):
        return {str(key): _encode_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_encode_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"save-state value is not serializable: {type(value)!r}")


def _decode_value(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"__bytes__"}:
            return base64.b85decode(value["__bytes__"].encode("ascii"))
        return {key: _decode_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_value(item) for item in value]
    return value


def dumps(payload: dict, *, compression_level: int = 9) -> bytes:
    if not 0 <= int(compression_level) <= 9:
        raise ValueError("compression_level must be between 0 and 9")
    envelope = {
        "format": "nes-from-scratch",
        "version": FORMAT_VERSION,
        "payload": _encode_value(payload),
    }
    raw = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return MAGIC + zlib.compress(raw, level=int(compression_level))


def loads(data: bytes) -> dict:
    if not data.startswith(MAGIC):
        raise StateError("not a nes-from-scratch save state")
    try:
        envelope = json.loads(zlib.decompress(data[len(MAGIC):]))
    except (zlib.error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateError("save state is corrupt") from exc
    if envelope.get("format") != "nes-from-scratch":
        raise StateError("unknown save-state format")
    if envelope.get("version") != FORMAT_VERSION:
        raise StateError(
            f"unsupported save-state version {envelope.get('version')!r}"
        )
    payload = _decode_value(envelope.get("payload"))
    if not isinstance(payload, dict):
        raise StateError("save state has no valid payload")
    return payload


def save_atomic(path: str | Path, payload: dict) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = dumps(payload)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
