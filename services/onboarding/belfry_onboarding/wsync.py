"""websockets' synchronous client, made safe to close from another thread.

Every sync WebSocket in this service goes through :func:`connect` here, never through
``websockets.sync.client.connect`` directly.

Why: websockets 17.3 changed ``Connection.close_socket()`` from ``shutdown(SHUT_RDWR)`` +
``close()`` to a wake-up through a socketpair + ``close()``. The socketpair only wakes the reader
thread while it waits in ``select()``. Over TLS the reader is often blocked *inside*
``SSL_read`` instead (TLS 1.3 session tickets make the socket readable, OpenSSL consumes them and
then blocks in ``read()`` waiting for the next record, e.g. the HTTP 101 that a slow relay never
sends). Closing the fd from the caller's thread then leaves OpenSSL holding a bare fd number:

1. ``connect()`` times out ("timed out while waiting for handshake response") and
   ``close_socket()`` closes fd N; the reader is still blocked in ``read(N)`` on the old socket;
2. another thread opens an SQLite file and gets fd N;
3. the peer finally sends something (Caddy's close_notify when it closes the connection): the
   blocked ``read()`` returns the 5-byte record header, OpenSSL ``read()``s the 19-byte body from
   fd N, which is now the database (bytes 0-18, SQLite keeps its file offset at 0), fails to
   decrypt it and ``write()``s a fatal alert record (``17 03 03 00 13`` + 19 bytes) at offset 19.

That is exactly the damage seen in devices.sqlite, sales.sqlite and events.sqlite on every box.
``tests/test_wsync.py`` reproduces it with the real websockets and checks this module.

The fix restores what websockets <= 17.2 did: ``shutdown(SHUT_RDWR)`` before anything else, which
interrupts a ``read()`` blocked in any thread (``close()`` does not, on Linux), and then lets the
reader thread close the socket itself when it can, so no other thread closes an fd that OpenSSL
may still read or write.
"""

from __future__ import annotations

import socket
import threading
from typing import Any

from websockets.sync.client import ClientConnection
from websockets.sync.client import connect as _connect

# How long a closing thread waits for the reader to close the socket itself before closing it
# like websockets does. After shutdown() the reader wakes at once; it only stays when it waits for
# the application to read queued messages (the base close_socket() releases that).
READER_GRACE = 0.5


class SafeClientConnection(ClientConnection):
    def close_socket(self) -> None:
        # shutdown() interrupts a read() or write() blocked in another thread (and sends the FIN):
        # after it, nobody is left inside OpenSSL with this fd.
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except (OSError, ValueError):  # already shut down or closed
            pass
        reader = getattr(self, "recv_events_thread", None)
        if reader is not None and reader is not threading.current_thread() and reader.is_alive():
            # The reader closes the socket in its own finally: give it the chance, so the fd is
            # closed by the only thread that reads it.
            reader.join(READER_GRACE)
        super().close_socket()


def connect(uri: str | None = None, **kwargs: Any) -> ClientConnection:
    """``websockets.sync.client.connect`` with :class:`SafeClientConnection`."""
    kwargs.setdefault("create_connection", SafeClientConnection)
    return _connect(uri, **kwargs)
