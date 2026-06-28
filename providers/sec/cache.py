from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
import hashlib


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return {"__type__": "datetime", "value": value.astimezone(UTC).isoformat()}
    if hasattr(value, "isoformat") and callable(value.isoformat):
        return {"__type__": "date", "value": value.isoformat()}
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "__dict__"):
        return _json_safe(vars(value))
    raise TypeError(f"Unsupported cache value type: {type(value)!r}")


def _json_restore(value: Any) -> Any:
    if isinstance(value, dict):
        value_type = value.get("__type__")
        if value_type == "datetime":
            return datetime.fromisoformat(str(value.get("value", "")))
        if value_type == "date":
            return datetime.fromisoformat(f"{value.get('value')}T00:00:00+00:00").date()
        return {key: _json_restore(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_restore(item) for item in value]
    return value


class JSONDiskCache:
    def __init__(self, root: Path, *, default_ttl: timedelta | None = None, enabled: bool = True) -> None:
        self.root = Path(root)
        self.default_ttl = default_ttl
        self.enabled = enabled

    def _path_for_key(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / digest[:2] / f"{digest}.json"

    def get(self, key: str, *, ttl: timedelta | None = None) -> Any | None:
        if not self.enabled:
            return None
        path = self._path_for_key(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return None
        expires_at = payload.get("expires_at")
        if expires_at:
            try:
                expiry = datetime.fromisoformat(str(expires_at))
            except ValueError:
                expiry = None
            if expiry is not None and expiry < datetime.now(UTC):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                return None
        elif ttl is not None:
            saved_at = datetime.fromisoformat(str(payload.get("saved_at")))
            if saved_at + ttl < datetime.now(UTC):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                return None
        return _json_restore(payload.get("value"))

    def set(self, key: str, value: Any, *, ttl: timedelta | None = None) -> None:
        if not self.enabled:
            return
        ttl = self.default_ttl if ttl is None else ttl
        path = self._path_for_key(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        payload = {
            "key": key,
            "saved_at": now.isoformat(),
            "expires_at": (now + ttl).isoformat() if ttl is not None else None,
            "value": _json_safe(value),
        }
        with NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            temp_path = Path(handle.name)
        temp_path.replace(path)

