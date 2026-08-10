"""Unit tests for KVClient — RESP command encoding + reply parsing over a
faked socket (no TCP, no Redis)."""

import io

import pytest

from api.kv_client import KVClient, KVError, RedisCommandError


@pytest.fixture
def fake_socket(monkeypatch):
    """Patch socket.create_connection to hand back an in-memory FakeSocket.

    `state["reply"]` is the raw bytes the "server" returns for the connection
    (prefix an AUTH `+OK\\r\\n` when the URL carries a password). `state["raise"]`
    simulates a connection failure.
    """
    state = {"reply": b"+OK\r\n", "sockets": [], "raise": None}

    class FakeSocket:
        def __init__(self, reply):
            self.sent = bytearray()
            self._reader = io.BytesIO(reply)
            self.closed = False

        def sendall(self, data):
            self.sent += data

        def makefile(self, mode="rb"):
            return self._reader

        def close(self):
            self.closed = True

    def fake_create_connection(addr, timeout=None):
        if state["raise"] is not None:
            raise state["raise"]
        sock = FakeSocket(state["reply"])
        state["sockets"].append(sock)
        state["last_addr"] = addr
        return sock

    import api.kv_client as mod
    monkeypatch.setattr(mod.socket, "create_connection", fake_create_connection)
    return state


def _sent(state) -> bytes:
    return bytes(state["sockets"][-1].sent)


def test_set_encodes_resp_array(fake_socket):
    KVClient(url="redis://h:6379").set("k", "v")
    assert _sent(fake_socket) == b"*3\r\n$3\r\nSET\r\n$1\r\nk\r\n$1\r\nv\r\n"
    assert fake_socket["last_addr"] == ("h", 6379)


def test_set_with_ex_appends_expiry(fake_socket):
    KVClient(url="redis://h:6379").set("k", "v", ex=15)
    assert _sent(fake_socket) == (
        b"*5\r\n$3\r\nSET\r\n$1\r\nk\r\n$1\r\nv\r\n$2\r\nEX\r\n$2\r\n15\r\n")


def test_get_returns_bulk_string(fake_socket):
    fake_socket["reply"] = b"$5\r\nhello\r\n"
    assert KVClient(url="redis://h:6379").get("k") == "hello"
    assert _sent(fake_socket) == b"*2\r\n$3\r\nGET\r\n$1\r\nk\r\n"


def test_get_missing_key_returns_none(fake_socket):
    fake_socket["reply"] = b"$-1\r\n"
    assert KVClient(url="redis://h:6379").get("nope") is None


def test_set_nx_true_when_ok(fake_socket):
    fake_socket["reply"] = b"+OK\r\n"
    assert KVClient(url="redis://h:6379").set_nx("lock", "1", ex=15) is True
    assert _sent(fake_socket) == (
        b"*6\r\n$3\r\nSET\r\n$4\r\nlock\r\n$1\r\n1\r\n"
        b"$2\r\nNX\r\n$2\r\nEX\r\n$2\r\n15\r\n")


def test_set_nx_false_when_null(fake_socket):
    fake_socket["reply"] = b"$-1\r\n"
    assert KVClient(url="redis://h:6379").set_nx("lock", "1", ex=15) is False


def test_delete_encodes_del_and_reads_integer(fake_socket):
    fake_socket["reply"] = b":1\r\n"
    KVClient(url="redis://h:6379").delete("k")
    assert _sent(fake_socket) == b"*2\r\n$3\r\nDEL\r\n$1\r\nk\r\n"


def test_auth_is_sent_before_command_when_password_present(fake_socket):
    # AUTH's +OK, then the GET's bulk-string reply, on one connection.
    fake_socket["reply"] = b"+OK\r\n$3\r\nabc\r\n"
    result = KVClient(url="redis://default:secret@h:6379").get("k")
    assert result == "abc"
    sent = _sent(fake_socket)
    assert sent.startswith(
        b"*3\r\n$4\r\nAUTH\r\n$7\r\ndefault\r\n$6\r\nsecret\r\n")
    assert sent.endswith(b"*2\r\n$3\r\nGET\r\n$1\r\nk\r\n")


def test_tls_scheme_wraps_socket_with_sni(fake_socket, monkeypatch):
    wrapped = {}

    class FakeCtx:
        def wrap_socket(self, sock, server_hostname=None):
            wrapped["host"] = server_hostname
            return sock

    import api.kv_client as mod
    monkeypatch.setattr(mod.ssl, "create_default_context", lambda: FakeCtx())
    fake_socket["reply"] = b"+OK\r\n$1\r\nx\r\n"  # AUTH ok + GET

    KVClient(url="rediss://default:tok@kv.example:6379").get("k")
    assert wrapped["host"] == "kv.example"


def test_error_reply_raises_command_error(fake_socket):
    fake_socket["reply"] = b"-WRONGPASS invalid password\r\n"
    with pytest.raises(RedisCommandError, match="WRONGPASS"):
        KVClient(url="redis://h:6379").get("k")


def test_transport_failure_raises_kverror(fake_socket):
    fake_socket["raise"] = OSError("connection refused")
    with pytest.raises(KVError):
        KVClient(url="redis://h:6379").get("k")


def test_kverror_is_urlerror_subclass():
    from urllib import error as urlerror
    assert issubclass(KVError, urlerror.URLError)
