"""A sync WebSocket closed after a handshake timeout must never write into a file that reused
its fd (the "file is not a database" damage of 2026-11-24/25: a TLS alert at offset 19)."""

from __future__ import annotations

import datetime as dt
import socket
import sqlite3
import ssl
import threading
import time
from pathlib import Path

import pytest
import websockets
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from websockets.sync.client import connect as raw_connect

from belfry_onboarding import wsync

OPEN_TIMEOUT = 0.5  # the client gives up on the HTTP 101 after this
VICTIM_AT = 1.2  # then another thread opens a database (and gets the freed fd number)
PEER_CLOSES_AT = 2.2  # then the server closes TLS: close_notify, a 24-byte record


def _cert(tmp: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp / "cert.pem", tmp / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def _silent_tls_server(lsock: socket.socket, cert: Path, key: Path, start: float) -> None:
    """Finishes TLS 1.3 (session tickets included), reads the upgrade request, never answers it,
    and closes TLS later, like a relay that is slow to start behind Caddy."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    raw, _ = lsock.accept()
    try:
        tls = ctx.wrap_socket(raw, server_side=True)
        tls.recv(4096)
        time.sleep(max(0.0, PEER_CLOSES_AT - (time.monotonic() - start)))
        try:
            tls.unwrap()
        except (OSError, ssl.SSLError):
            pass
        tls.close()
    except (OSError, ssl.SSLError):
        raw.close()


def _run(tmp_path: Path, connect) -> tuple[bytes, bytes]:
    db_path = tmp_path / "victim.sqlite"
    db = sqlite3.connect(db_path)
    db.execute("create table t(x)")
    db.execute("insert into t values (1)")
    db.commit()
    db.close()
    healthy = db_path.read_bytes()[:100]
    cert, key = _cert(tmp_path)

    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen()
    port = lsock.getsockname()[1]
    start = time.monotonic()
    server = threading.Thread(target=_silent_tls_server, args=(lsock, cert, key, start))
    server.start()

    victim_dbs: list[sqlite3.Connection] = []

    def victim() -> None:
        time.sleep(VICTIM_AT)
        conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        conn.execute("select count(*) from t").fetchone()
        victim_dbs.append(conn)  # kept open past the peer's close

    threading.Thread(target=victim, daemon=True).start()
    client_ctx = ssl.create_default_context(cafile=str(cert))
    with pytest.raises(TimeoutError):
        with connect(f"wss://localhost:{port}/", ssl=client_ctx, open_timeout=OPEN_TIMEOUT):
            pass
    server.join(10)
    time.sleep(0.3)
    for conn in victim_dbs:
        conn.close()
    lsock.close()
    assert healthy.startswith(b"SQLite format 3\x00")
    return healthy, db_path.read_bytes()[:100]


def test_a_timed_out_handshake_never_writes_into_a_reused_fd(tmp_path):
    healthy, after = _run(tmp_path, wsync.connect)
    assert after == healthy
    assert sqlite3.connect(tmp_path / "victim.sqlite").execute(
        "pragma integrity_check"
    ).fetchone() == ("ok",)


@pytest.mark.skipif(
    tuple(int(p) for p in websockets.__version__.split(".")[:2]) < (17, 3),
    reason="websockets before 17.3 shut the socket down before closing it",
)
def test_the_plain_websockets_client_does_damage_the_database(tmp_path):
    """Documents the bug the wrapper exists for; if this starts failing, websockets fixed it."""
    healthy, after = _run(tmp_path, raw_connect)
    if after == healthy:
        pytest.skip("this run's timing let the plain client get away with it")
    assert after[:19] == healthy[:19]
    assert after[19:24] == bytes.fromhex("1703030013")  # a TLS 1.3 alert record, 19 bytes long


def test_every_sync_websocket_goes_through_wsync():
    root = Path(__file__).resolve().parent.parent / "belfry_onboarding"
    offenders = [
        p.name
        for p in root.rglob("*.py")
        if p.name != "wsync.py" and "websockets.sync.client import connect" in p.read_text()
    ]
    assert offenders == []
