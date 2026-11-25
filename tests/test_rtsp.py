"""RTSP login diagnosis against a fake camera that speaks Digest auth."""

from __future__ import annotations

import hashlib
import re
import socket
import threading

import pytest

from studio import projects, sources
from studio.sources import rtsp
from studio.sources.probe import _url_with_password

USER, PASSWORD, REALM, NONCE = "admin", "Admin@123", "IP Camera", "abc123"
STATS = {"unauth": 0, "refused": 0}


def _serve(sock: socket.socket) -> None:
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        with conn:
            buf = b""
            while True:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\r\n\r\n" in buf:
                    req, buf = buf.split(b"\r\n\r\n", 1)
                    text = req.decode()
                    first = text.split("\r\n")[0]
                    method, uri = first.split()[:2]
                    cseq = re.search(r"CSeq: (\d+)", text).group(1)
                    auth = re.search(r"Authorization: (.*)", text)
                    ok = False
                    if auth:
                        f = dict(re.findall(r'(\w+)="([^"]*)"', auth.group(1)))
                        ha1 = hashlib.md5(f"{f.get('username')}:{REALM}:{PASSWORD}".encode()).hexdigest()
                        ha2 = hashlib.md5(f"{method}:{f.get('uri')}".encode()).hexdigest()
                        ok = f.get("response") == hashlib.md5(f"{ha1}:{NONCE}:{ha2}".encode()).hexdigest() and f.get("username") == USER
                    if not ok:
                        STATS["refused" if auth else "unauth"] += 1
                        resp = (f"RTSP/1.0 401 Unauthorized\r\nCSeq: {cseq}\r\n"
                                f'WWW-Authenticate: Digest realm="{REALM}", nonce="{NONCE}"\r\n\r\n')
                    elif not uri.endswith("/Streaming/Channels/101"):
                        resp = f"RTSP/1.0 404 Not Found\r\nCSeq: {cseq}\r\n\r\n"
                    else:
                        resp = f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nContent-Length: 0\r\n\r\n"
                    conn.sendall(resp.encode())


@pytest.fixture
def camera():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    threading.Thread(target=_serve, args=(sock,), daemon=True).start()
    yield sock.getsockname()[1]
    sock.close()


def test_describe_with_digest(camera):
    url = f"rtsp://admin@127.0.0.1:{camera}/Streaming/Channels/101"
    assert rtsp.describe(url, USER, PASSWORD).status == 200
    assert rtsp.describe(url, USER, "wrong").status == 401
    assert rtsp.describe(url, None, None).auth == "digest"
    assert rtsp.describe(url.replace("101", "999"), USER, PASSWORD).status == 404


def test_connection_explains_401_and_404(camera, workspace):
    slug = projects.create_project("Cam")["slug"]
    wrong = sources.add_source(slug, "ip", "wrong", url=f"rtsp://admin:nope@127.0.0.1:{camera}/Streaming/Channels/101")
    r = sources.test_connection(slug, wrong["id"])
    assert r["ok"] is False and "401" in r["error_en"] and "admin" in r["error_ar"]
    path = sources.add_source(slug, "ip", "path", url=f"rtsp://admin@127.0.0.1:{camera}/bad", password=PASSWORD)
    r = sources.test_connection(slug, path["id"])
    assert r["ok"] is False and "404" in r["error_en"]
    nouser = sources.add_source(slug, "ip", "nouser", url=f"rtsp://127.0.0.1:{camera}/Streaming/Channels/101")
    r = sources.test_connection(slug, nouser["id"])
    assert "user name" in r["error_en"]


def test_password_is_written_the_way_ffmpeg_reads_it():
    url = "rtsp://admin@10.0.0.5:554/Streaming/Channels/101"
    assert _url_with_password(url, "Admin@123") == "rtsp://admin:Admin@123@10.0.0.5:554/Streaming/Channels/101"
    assert _url_with_password(url, "a/b#c", "encoded") == "rtsp://admin:a%2Fb%23c@10.0.0.5:554/Streaming/Channels/101"
    assert _url_with_password("rtsp://us%40er@h/x", "p") == "rtsp://us@er:p@h/x"


def test_update_login_fixes_a_refused_camera(camera, workspace, memory_keyring):
    slug = projects.create_project("Cam2")["slug"]
    src = sources.add_source(slug, "ip", "cam", url=f"rtsp://admin:nope@127.0.0.1:{camera}/Streaming/Channels/101")
    assert "401" in sources.test_connection(slug, src["id"])["error_en"]
    updated = sources.update_login(slug, src["id"], user="admin", password=PASSWORD)
    assert updated["params"]["url"] == f"rtsp://admin@127.0.0.1:{camera}/Streaming/Channels/101"
    assert memory_keyring.get_password("inno-vision-studio", f"{slug}:{src['id']}") == PASSWORD
    assert rtsp.describe(updated["params"]["url"], "admin", PASSWORD).status == 200


def test_find_and_use_stream_path(camera, workspace):
    slug = projects.create_project("Cam3")["slug"]
    src = sources.add_source(slug, "ip", "cam", url=f"rtsp://admin@127.0.0.1:{camera}/wrong/path", password=PASSWORD)
    assert "404" in sources.test_connection(slug, src["id"])["error_en"]
    result = sources.find_stream_paths(slug, src["id"])
    assert result["tried"] == len(rtsp.COMMON_PATHS)
    assert [f["url_path"] for f in result["found"]] == ["/Streaming/Channels/101"]
    updated = sources.use_stream_path(slug, src["id"], "/Streaming/Channels/101")
    assert updated["params"]["url"] == f"rtsp://admin@127.0.0.1:{camera}/Streaming/Channels/101"
    assert rtsp.describe(updated["params"]["url"], USER, PASSWORD).status == 200


def test_password_in_path_stays_a_placeholder():
    url = "rtsp://admin@10.0.0.9:554/user=admin&password={password}&channel=1&stream=0.sdp"
    assert rtsp.fill(url, "admin", "s3cret").endswith("password=s3cret&channel=1&stream=0.sdp")
    assert "s3cret" in _url_with_password(url, "s3cret") and "{password}" not in _url_with_password(url, "s3cret")


def test_path_search_uses_one_login_and_stops_when_refused(camera, workspace):
    """Cameras lock the account after a few refused or unauthenticated requests."""
    STATS.update(unauth=0, refused=0)
    found = rtsp.find_paths(f"rtsp://admin@127.0.0.1:{camera}/x", USER, PASSWORD)
    assert [f["url_path"] for f in found] == ["/Streaming/Channels/101"]
    assert STATS["unauth"] <= 2 and STATS["refused"] == 0, STATS
    STATS.update(unauth=0, refused=0)
    with pytest.raises(rtsp.LoginRefused):
        rtsp.find_paths(f"rtsp://admin@127.0.0.1:{camera}/x", USER, "wrong")
    assert STATS["refused"] == 1, "one refused login, then stop"
    slug = projects.create_project("Cam4")["slug"]
    src = sources.add_source(slug, "ip", "cam", url=f"rtsp://admin@127.0.0.1:{camera}/x", password="wrong")
    with pytest.raises(sources.SourceError, match="stopped"):
        sources.find_stream_paths(slug, src["id"])
