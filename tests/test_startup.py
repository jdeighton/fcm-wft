import socket

from wft.__main__ import HOST, bind_socket


def test_bind_uses_preferred_port_when_free():
    probe = socket.socket()
    probe.bind((HOST, 0))
    port = probe.getsockname()[1]
    probe.close()
    sock = bind_socket(port)
    try:
        assert sock.getsockname() == (HOST, port)
    finally:
        sock.close()


def test_bind_falls_back_to_free_port_when_taken():
    taken = socket.socket()
    taken.bind((HOST, 0))
    taken.listen()
    port = taken.getsockname()[1]
    try:
        sock = bind_socket(port)
        try:
            assert sock.getsockname()[0] == HOST and sock.getsockname()[1] != port
        finally:
            sock.close()
    finally:
        taken.close()
