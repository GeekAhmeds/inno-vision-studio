"""Source definitions (prompt §5.2). One YAML file per source in `<project>/sources/`.

A definition never contains a password. If the owner pastes `rtsp://user:pass@host/...`, the
password is moved to the keyring and the stored URL keeps only the user name.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

from ..explain import activity
from ..projects import project_dir
from . import secrets

KINDS: dict[str, dict[str, str]] = {
    "ip": {"ar": "كاميرا IP (RTSP / MJPEG)", "en": "IP camera (RTSP / MJPEG)"},
    "nvr": {"ar": "قناة على NVR / DVR", "en": "NVR / DVR channel"},
    "usb": {"ar": "كاميرا USB أو كاميرا الجهاز", "en": "USB or built-in camera"},
    "video": {"ar": "ملف فيديو", "en": "Video file"},
}


class SourceError(Exception):
    """A problem the owner can understand and fix."""


def _dir(slug: str) -> Path:
    return project_dir(slug) / "sources"


def split_credentials(url: str) -> tuple[str, str | None]:
    """Return (url without password, password or None)."""
    parts = urlsplit(url)
    if not parts.password:
        return url, None
    host = parts.hostname or ""
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    netloc = f"{parts.username}@{host}" if parts.username else host
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment)), parts.password


def add_source(slug: str, kind: str, name: str, *, url: str = "", device: int | None = None,
               path: str = "", password: str | None = None) -> dict[str, Any]:
    if kind not in KINDS:
        raise SourceError(f"Unknown source kind: {kind}")
    name = (name or "").strip() or KINDS[kind]["en"]
    source_id = f"src-{uuid.uuid4().hex[:8]}"
    params: dict[str, Any] = {}
    if kind in ("ip", "nvr"):
        if not re.match(r"^(rtsp|rtsps|http|https)://", url or ""):
            raise SourceError("The camera address must start with rtsp:// or http://")
        clean, embedded = split_credentials(url)
        params["url"] = clean
        password = password or embedded
    elif kind == "usb":
        if device is None or device < 0:
            raise SourceError("Choose which USB camera to use.")
        params["device"] = int(device)
    elif kind == "video":
        if not path or not Path(path).is_file():
            raise SourceError(f"Video file not found: {path}")
        params["path"] = str(Path(path))
    if password:
        secrets.set_password(slug, source_id, password)
    source = {"id": source_id, "kind": kind, "name": name, "params": params, "has_password": bool(password)}
    (_dir(slug) / f"{source_id}.yaml").write_text(
        yaml.safe_dump(source, allow_unicode=True, sort_keys=False), encoding="utf-8")
    activity.log(project_dir(slug), "source.add",
                 f"أُضيف مصدر «{name}» ({KINDS[kind]['ar']}).",
                 f"Source “{name}” added ({KINDS[kind]['en']}).",
                 why_ar="كلمة السر (إن وُجدت) محفوظة في خزنة النظام وليست في ملفات المشروع.",
                 why_en="The password, if any, is kept in the system keyring, not in project files.",
                 outputs={"source_id": source_id})
    return source


def list_sources(slug: str) -> list[dict[str, Any]]:
    return [yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted(_dir(slug).glob("src-*.yaml"))]


def get_source(slug: str, source_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"src-[0-9a-f]{8}", source_id or ""):
        raise SourceError(f"Invalid source id: {source_id!r}")
    path = _dir(slug) / f"{source_id}.yaml"
    if not path.exists():
        raise SourceError(f"Source not found: {source_id}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def update_login(slug: str, source_id: str, url: str | None = None, user: str | None = None,
                 password: str | None = None) -> dict[str, Any]:
    """Change a camera's address, user name or password without removing the source."""
    from urllib.parse import quote

    source = get_source(slug, source_id)
    if source["kind"] not in ("ip", "nvr"):
        raise SourceError("Only network cameras have a login.")
    current = source["params"]["url"]
    if url:
        if not re.match(r"^(rtsp|rtsps|http|https)://", url):
            raise SourceError("The camera address must start with rtsp:// or http://")
        current, embedded = split_credentials(url)
        password = password or embedded
    if user is not None:
        parts = urlsplit(current)
        host = parts.netloc.rsplit("@", 1)[-1]
        netloc = f"{quote(user.strip(), safe='')}@{host}" if user.strip() else host
        current = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    source["params"]["url"] = current
    source["params"].pop("credential_style", None)
    if password:
        secrets.set_password(slug, source_id, password)
        source["has_password"] = True
    (_dir(slug) / f"{source_id}.yaml").write_text(yaml.safe_dump(source, allow_unicode=True, sort_keys=False), encoding="utf-8")
    activity.log(project_dir(slug), "source.login", f"عُدّلت بيانات الدخول للمصدر «{source['name']}».",
                 f"Login of source “{source['name']}” changed.")
    return source


def remove_source(slug: str, source_id: str) -> None:
    source = get_source(slug, source_id)
    secrets.delete_password(slug, source_id)
    (_dir(slug) / f"{source_id}.yaml").unlink()
    (_dir(slug) / f"{source_id}_preview.jpg").unlink(missing_ok=True)
    activity.log(project_dir(slug), "source.remove", f"حُذف المصدر «{source['name']}».",
                 f"Source “{source['name']}” removed.")
