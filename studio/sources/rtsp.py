"""A small RTSP probe: ask the camera for its stream description, with Basic or Digest login.

OpenCV only says "could not open". The camera's own answer tells the owner what is wrong:
401 = user name or password refused, 404 = wrong stream path, 200 = fine.
"""

from __future__ import annotations

import base64
import hashlib
import re
import socket
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit, urlunsplit


@dataclass
class RtspAnswer:
    status: int
    reason: str
    auth: str = ""          # "", "basic" or "digest": what the camera asked for


def _url_without_userinfo(url: str) -> str:
    p = urlsplit(url)
    host = p.netloc.rsplit("@", 1)[-1]
    return urlunsplit((p.scheme, host, p.path or "/", p.query, ""))


def _request(sock: socket.socket, method: str, url: str, cseq: int, extra: str = "") -> tuple[int, str, dict[str, str]]:
    msg = f"{method} {url} RTSP/1.0\r\nCSeq: {cseq}\r\nUser-Agent: InnoVisionStudio\r\nAccept: application/sdp\r\n{extra}\r\n"
    sock.sendall(msg.encode())
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    head = data.split(b"\r\n\r\n", 1)[0].decode(errors="replace").split("\r\n")
    m = re.match(r"RTSP/1\.\d\s+(\d{3})\s*(.*)", head[0] if head else "")
    if not m:
        raise OSError("The device did not answer as an RTSP camera.")
    headers: dict[str, str] = {}
    for line in head[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            key = k.strip().lower()
            # a camera may offer several WWW-Authenticate lines: keep Digest if there is one
            if key in headers and key == "www-authenticate" and not v.strip().lower().startswith("digest"):
                continue
            headers[key] = v.strip()
    return int(m.group(1)), m.group(2).strip(), headers


def _digest(challenge: str, user: str, password: str, method: str, uri: str) -> str:
    fields = dict(re.findall(r'(\w+)="?([^",]*)"?', challenge))
    realm, nonce = fields.get("realm", ""), fields.get("nonce", "")
    ha1 = hashlib.md5(f"{user}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    if "auth" in fields.get("qop", ""):
        nc, cnonce = "00000001", hashlib.md5(nonce.encode()).hexdigest()[:16]
        resp = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}".encode()).hexdigest()
        return (f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{uri}", response="{resp}", '
                f'qop=auth, nc={nc}, cnonce="{cnonce}"')
    resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    return f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{uri}", response="{resp}"'


def describe(url: str, user: str | None, password: str | None, timeout: float = 4.0) -> RtspAnswer:
    """Send DESCRIBE; if the camera asks for a login and we have one, answer it once."""
    url = fill(url, user, password) if "{" in url else url
    p = urlsplit(url)
    clean = _url_without_userinfo(url)
    with socket.create_connection((p.hostname, p.port or 554), timeout=timeout) as sock:
        sock.settimeout(timeout)
        status, reason, headers = _request(sock, "DESCRIBE", clean, 1)
        challenge = headers.get("www-authenticate", "")
        kind = "digest" if challenge.lower().startswith("digest") else "basic" if challenge else ""
        if status != 401 or not user:
            return RtspAnswer(status, reason, kind)
        if kind == "digest":
            auth = _digest(challenge, user, password or "", "DESCRIBE", clean)
        else:
            auth = "Basic " + base64.b64encode(f"{user}:{password or ''}".encode()).decode()
        status, reason, _ = _request(sock, "DESCRIBE", clean, 2, f"Authorization: {auth}\r\n")
        return RtspAnswer(status, reason, kind)


def credentials(url: str, password: str | None) -> tuple[str | None, str | None]:
    p = urlsplit(url)
    user = unquote(p.username) if p.username else None
    return user, password


# Stream paths used by common camera and NVR makers (channel 1). {user}/{password} are filled in
# for makers that put the login in the path (XMeye-based cameras, common in Egypt).
COMMON_PATHS: list[tuple[str, str]] = [
    ("Hikvision / HiLook (main)", "/Streaming/Channels/101"),
    ("Hikvision / HiLook (sub)", "/Streaming/Channels/102"),
    ("Hikvision (old)", "/h264/ch1/main/av_stream"),
    ("Hikvision", "/Streaming/Channels/1"),
    ("Dahua / Imou / Amcrest (main)", "/cam/realmonitor?channel=1&subtype=0"),
    ("Dahua / Imou / Amcrest (sub)", "/cam/realmonitor?channel=1&subtype=1"),
    ("Uniview (main)", "/unicast/c1/s0/live"),
    ("Uniview", "/media/video1"),
    ("TP-Link Tapo / VIGI (main)", "/stream1"),
    ("TP-Link Tapo / VIGI (sub)", "/stream2"),
    ("Reolink (main)", "/h264Preview_01_main"),
    ("Reolink (sub)", "/h264Preview_01_sub"),
    ("Reolink", "/Preview_01_main"),
    ("XMeye / Xiongmai (main)", "/user={user}&password={password}&channel=1&stream=0.sdp"),
    ("XMeye / Xiongmai (alt)", "/user={user}_password={password}_channel=1_stream=0.sdp"),
    ("Axis", "/axis-media/media.amp"),
    ("ONVIF generic", "/onvif1"),
    ("Generic", "/live"),
    ("Generic", "/live/ch0"),
    ("Generic", "/live/ch00_0"),
    ("Generic", "/live/main"),
    ("Generic", "/ch0_0.h264"),
    ("Generic", "/av0_0"),
    ("Generic", "/11"),
    ("Generic", "/12"),
    ("Generic", "/1"),
    ("Generic", "/0"),
    ("Generic", "/h264"),
    ("Generic", "/video1"),
    ("Generic", "/stream"),
    ("Generic", "/"),
]


def fill(url: str, user: str | None, password: str | None) -> str:
    """Stored URLs keep "{password}" where a maker wants the login in the path, so the password
    never lands in project files; it is filled in from the keyring only when the camera is opened."""
    return url.replace("{user}", user or "admin").replace("{password}", password or "")


def find_paths(url: str, user: str | None, password: str | None, timeout: float = 3.0) -> list[dict[str, object]]:
    """Ask the camera for each common path; return the ones it serves (status 200)."""
    p = urlsplit(url)
    host = p.netloc.rsplit("@", 1)[-1]
    found = []
    for maker, template in COMMON_PATHS:
        stored = template.replace("{user}", user or "admin")          # "{password}" stays a placeholder
        path, _, query = stored.partition("?")
        candidate = fill(urlunsplit((p.scheme, host, path, query, "")), user, password)
        try:
            answer = describe(candidate, user, password, timeout)
        except OSError:
            continue
        if answer.status == 200:
            found.append({"maker": maker, "path": stored.replace("{password}", "******"), "url_path": stored,
                          "has_login_in_path": "{password}" in template})
    return found
