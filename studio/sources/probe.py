"""Open a source, test it, and record from it (prompt §5.2).

OpenCV is the one capture layer on both systems: FFmpeg for network streams and files,
V4L2 for USB cameras on Linux, DirectShow on Windows.
"""

from __future__ import annotations

import json
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import os
from urllib.parse import quote, unquote, urlsplit, urlunsplit

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")   # keep OpenCV's own warnings out of the terminal
import cv2  # noqa: E402

from ..explain import activity  # noqa: E402
from ..projects import project_dir  # noqa: E402
from . import rtsp, secrets  # noqa: E402
from .store import SourceError, _dir, get_source  # noqa: E402

DEFAULT_PORTS = {"rtsp": 554, "rtsps": 322, "http": 80, "https": 443}


def _usb_backend() -> int:
    if sys.platform.startswith("win"):
        return cv2.CAP_DSHOW
    if sys.platform.startswith("linux"):
        return cv2.CAP_V4L2
    return cv2.CAP_ANY


def _url_with_password(url: str, password: str | None, style: str = "raw") -> str:
    """Put the login into the URL the way FFmpeg reads it.

    FFmpeg's RTSP client does not decode %-escapes in the login, so a password like "Admin@123"
    must go in as is ("raw"); FFmpeg takes the LAST "@" before the host. Only a password containing
    "/", "?" or "#" cannot be written raw; then the escaped form is tried."""
    if "{" in url:
        parts = urlsplit(url)
        url = rtsp.fill(url, unquote(parts.username) if parts.username else None, password)
    if not password:
        return url
    parts = urlsplit(url)
    user = unquote(parts.username) if parts.username else "admin"
    host = parts.netloc.rsplit("@", 1)[-1]
    if style == "encoded":
        login = f"{quote(user, safe='')}:{quote(password, safe='')}"
    else:
        login = f"{user}:{password}"
    return urlunsplit((parts.scheme, f"{login}@{host}", parts.path, parts.query, parts.fragment))


def _styles(password: str | None) -> list[str]:
    if not password:
        return ["raw"]
    return ["encoded", "raw"] if any(ch in password for ch in "/?#") else ["raw", "encoded"]


def _reachable(url: str, timeout: float = 3.0) -> bool:
    parts = urlsplit(url)
    port = parts.port or DEFAULT_PORTS.get(parts.scheme, 554)
    try:
        with socket.create_connection((parts.hostname, port), timeout=timeout):
            return True
    except OSError:
        return False


def open_capture(slug: str, source: dict[str, Any]) -> cv2.VideoCapture:
    kind, params = source["kind"], source["params"]
    if kind in ("ip", "nvr"):
        password = secrets.get_password(slug, source["id"])
        style = params.get("credential_style") or _styles(password)[0]
        return cv2.VideoCapture(_url_with_password(params["url"], password, style), cv2.CAP_FFMPEG)
    if kind == "usb":
        return cv2.VideoCapture(int(params["device"]), _usb_backend())
    return cv2.VideoCapture(params["path"])


def list_usb_cameras(max_devices: int = 6) -> list[dict[str, Any]]:
    """Probe device indexes. OpenCV has no portable way to read camera names, so the owner
    recognises a camera by its preview in Test connection."""
    found = []
    for index in range(max_devices):
        cap = cv2.VideoCapture(index, _usb_backend())
        try:
            if cap.isOpened():
                ok, frame = cap.read()
                if ok and frame is not None:
                    found.append({"device": index, "width": frame.shape[1], "height": frame.shape[0]})
        finally:
            cap.release()
    return found


def test_connection(slug: str, source_id: str, frames: int = 20) -> dict[str, Any]:
    """Open the source, read a few frames, save a preview. Errors are in plain words."""
    source = get_source(slug, source_id)
    kind, params = source["kind"], source["params"]
    folder = project_dir(slug)

    def fail(ar: str, en: str) -> dict[str, Any]:
        activity.log(folder, "source.test", f"فشل اختبار «{source['name']}»: {ar}",
                     f"Test of “{source['name']}” failed: {en}", outputs={"source_id": source_id, "ok": False})
        return {"ok": False, "error_ar": ar, "error_en": en}

    if kind in ("ip", "nvr") and not _reachable(params["url"]):
        return fail("الكاميرا غير متاحة على الشبكة. راجع العنوان والمنفذ وأن الجهاز على نفس الشبكة.",
                    "Camera unreachable. Check the address, the port, and that this PC is on the same network.")
    password = secrets.get_password(slug, source_id) if kind in ("ip", "nvr") else None
    if kind in ("ip", "nvr") and source.get("has_password") and password is None:
        return fail("كلمة سر الكاميرا غير موجودة في خزنة النظام (keyring). احذف المصدر وأضفه من جديد، "
                    "وتأكد أن خزنة النظام (GNOME Keyring / KWallet) مفتوحة.",
                    "The camera password is not in the system keyring. Remove and add the source again, "
                    "and make sure the keyring (GNOME Keyring / KWallet) is unlocked.")
    if kind in ("ip", "nvr") and params["url"].startswith("rtsp://"):
        user, _ = rtsp.credentials(params["url"], password)
        try:
            answer = rtsp.describe(params["url"], user, password)
        except OSError:
            answer = None   # not a plain RTSP server (or a timeout): let OpenCV try and report
        if answer is not None and answer.status == 401:
            if not user:
                return fail("الكاميرا تطلب اسم مستخدم وكلمة سر. احذف المصدر وأضفه بهما.",
                            "The camera asks for a user name and password. Remove the source and add it with them.")
            return fail(f"الكاميرا رفضت اسم المستخدم «{user}» أو كلمة السر (401). لو نفس البيانات كانت شغالة قبل كده، "
                        "فالكاميرا غالبًا قفلت الحساب مؤقتًا بعد محاولات كثيرة (Hikvision تقفله حوالي 30 دقيقة): "
                        "استنَّ نصف ساعة أو افتحه من صفحة الكاميرا، ثم جرّب مرة واحدة.",
                        f"The camera refused user “{user}” or the password (401). Check them in the camera or NVR settings. "
                        "Some cameras lock the account for a while after several wrong tries.")
        if answer is not None and answer.status == 404:
            return fail("الكاميرا قبلت الدخول لكن مسار البث غير موجود (404). اضغط «ابحث عن مسار البث» ليجرّب البرنامج "
                        "المسارات المعروفة للماركات المشهورة.",
                        "Login accepted but the stream path does not exist (404). Press “Find the stream path” to try the "
                        "known paths of common makers.")
        if answer is not None and answer.status >= 400:
            return fail(f"الكاميرا ردّت بخطأ {answer.status} ({answer.reason}).",
                        f"The camera answered {answer.status} ({answer.reason}).")
    if kind == "video" and not Path(params["path"]).is_file():
        return fail("ملف الفيديو غير موجود في مكانه.", "The video file is no longer at its path.")

    if kind in ("ip", "nvr"):
        cap = None
        for style in _styles(password):   # find how this camera's FFmpeg login must be written
            cap = cv2.VideoCapture(_url_with_password(params["url"], password, style), cv2.CAP_FFMPEG)
            if cap.isOpened():
                if source["params"].get("credential_style") != style:
                    source["params"]["credential_style"] = style
                    import yaml
                    (_dir(slug) / f"{source_id}.yaml").write_text(
                        yaml.safe_dump(source, allow_unicode=True, sort_keys=False), encoding="utf-8")
                break
            cap.release()
    else:
        cap = open_capture(slug, source)
    try:
        if not cap.isOpened():
            if kind in ("ip", "nvr"):
                return fail("الكاميرا ردّت لكنها رفضت البث. راجع اسم المستخدم وكلمة السر ومسار البث.",
                            "The camera answered but refused the stream. Check the user name, password and stream path.")
            if kind == "usb":
                return fail("لم تُفتح الكاميرا. قد تكون مستخدمة من برنامج آخر أو مفصولة.",
                            "Could not open the camera. It may be in use by another program, or unplugged.")
            return fail("تعذّر فتح الفيديو. قد يكون الترميز غير مدعوم.",
                        "Could not open the video. The codec may be unsupported.")
        started, got, frame = time.monotonic(), 0, None
        for _ in range(frames):
            ok, current = cap.read()
            if not ok or current is None:
                break
            got, frame = got + 1, current
        elapsed = time.monotonic() - started
        if frame is None:
            return fail("فُتح المصدر لكن لم تصل أي صورة.", "The source opened but delivered no frame.")
        declared = cap.get(cv2.CAP_PROP_FPS) or 0.0
        # For a file, reading is faster than real time, so the declared rate is the true one.
        fps = declared if kind == "video" else (got / elapsed if elapsed > 0 else 0.0)
    finally:
        cap.release()

    preview = folder / "sources" / f"{source_id}_preview.jpg"
    cv2.imwrite(str(preview), frame)
    result = {
        "ok": True,
        "width": int(frame.shape[1]),
        "height": int(frame.shape[0]),
        "fps": round(float(fps), 2),
        "fps_declared": round(float(declared), 2),
        "frames_read": got,
        "preview": preview.relative_to(folder).as_posix(),
    }
    activity.log(folder, "source.test",
                 f"اختبار «{source['name']}» نجح: {result['width']}×{result['height']} بمعدل {result['fps']} صورة/ث.",
                 f"Test of “{source['name']}” passed: {result['width']}×{result['height']} at {result['fps']} fps.",
                 outputs={"source_id": source_id, "ok": True},
                 numbers={k: result[k] for k in ("width", "height", "fps", "frames_read")})
    return result


def record(slug: str, source_id: str, seconds: float, max_frames: int | None = None) -> Path:
    """Record a clip from a source into the project (live cameras are recorded first, so every
    result can be replayed). Writes the clip plus a sidecar with one timestamp per frame.

    The clip is NOT registered as media here; pass the returned path to `ingest.add_video`.
    """
    source = get_source(slug, source_id)
    folder = project_dir(slug)
    cap = open_capture(slug, source)
    if not cap.isOpened():
        cap.release()
        raise SourceError("Could not open the source to record. Run Test connection for the reason.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = folder / "media" / "videos" / f"rec_{source_id}_{stamp}.mp4"
    live = source["kind"] != "video"
    writer, stamps = None, []
    started = time.monotonic()
    try:
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            # Live: wall-clock capture time. File: the stream's own presentation time.
            t = time.monotonic() - started if live else cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
            if t > seconds or (max_frames and len(stamps) >= max_frames):
                break
            if writer is None:
                fps = cap.get(cv2.CAP_PROP_FPS) or 20.0
                writer = cv2.VideoWriter(str(out_path), cv2.VideoWriter_fourcc(*"mp4v"), fps,
                                         (frame.shape[1], frame.shape[0]))
            writer.write(frame)
            stamps.append(round(t, 4))
    finally:
        cap.release()
        if writer is not None:
            writer.release()
    if not stamps:
        raise SourceError("The source delivered no frames; nothing was recorded.")
    out_path.with_suffix(".timestamps.json").write_text(json.dumps({"seconds": stamps}), encoding="utf-8")
    activity.log(folder, "source.record",
                 f"سُجّل مقطع من «{source['name']}»: {len(stamps)} صورة في {stamps[-1]:.1f} ث.",
                 f"Recorded a clip from “{source['name']}”: {len(stamps)} frames over {stamps[-1]:.1f} s.",
                 why_ar="التدريب والقياس يتمّان على تسجيلات، حتى يمكن إعادة أي نتيجة.",
                 why_en="Training and measuring use recordings, so every result can be replayed.",
                 outputs={"clip": out_path.relative_to(folder).as_posix()},
                 numbers={"frames": len(stamps), "seconds": stamps[-1]})
    return out_path


def find_stream_paths(slug: str, source_id: str) -> dict[str, Any]:
    source = get_source(slug, source_id)
    url = source["params"]["url"]
    if source["kind"] not in ("ip", "nvr") or not url.startswith("rtsp://"):
        raise SourceError("Path search works for RTSP cameras only.")
    if not _reachable(url):
        raise SourceError("Camera unreachable. Check the address and the network first.")
    password = secrets.get_password(slug, source_id)
    user, _ = rtsp.credentials(url, password)
    try:
        found = rtsp.find_paths(url, user, password)
    except rtsp.LoginRefused:
        raise SourceError("The camera refused the login, so the search stopped (to avoid locking the account). "
                          "If the login worked before, the camera has probably locked the account after several tries: "
                          "wait 30 minutes, or unlock it from the camera's web page, then try once.") from None
    activity.log(project_dir(slug), "source.find_path",
                 f"جُرّب {len(rtsp.COMMON_PATHS)} مسار معروف على «{source['name']}»: يعمل منها {len(found)}.",
                 f"{len(rtsp.COMMON_PATHS)} known paths tried on “{source['name']}”: {len(found)} work.",
                 outputs={"paths": [f["path"] for f in found]})
    return {"tried": len(rtsp.COMMON_PATHS), "found": found}


def use_stream_path(slug: str, source_id: str, url_path: str) -> dict[str, Any]:
    """Keep host, port and user; replace the path (and query)."""
    from .store import update_login

    source = get_source(slug, source_id)
    p = urlsplit(source["params"]["url"])
    path, _, query = url_path.partition("?")
    if not path.startswith("/") or "{" in url_path.replace("{password}", ""):
        raise SourceError("Invalid stream path.")
    new_url = urlunsplit((p.scheme, p.netloc, path, query, ""))
    return update_login(slug, source_id, url=new_url)
