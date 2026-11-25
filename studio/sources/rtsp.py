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
