"""mail-in listens on IPv4 and IPv6 (an IPv4-only listener refused every IPv6 sender)."""

import asyncio
import socket

import pytest

from belfry_onboarding import mailfront


def _has_ipv6() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _has_ipv6(), reason="no IPv6 on this machine")
def test_mail_in_answers_on_ipv4_and_ipv6():
    async def run():
        loop = asyncio.get_running_loop()

        class Greeter(asyncio.Protocol):
            def connection_made(self, transport):
                transport.write(b"220 hi\r\n")
                transport.close()

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        server = await mailfront.listen(loop, Greeter, port=port)
        families = {sock.family for sock in server.sockets}
        assert families == {socket.AF_INET, socket.AF_INET6}
        for host in ("127.0.0.1", "::1"):
            reader, writer = await asyncio.open_connection(host, port)
            assert (await reader.readline()).startswith(b"220")
            writer.close()
        server.close()
        await server.wait_closed()

    asyncio.run(run())
