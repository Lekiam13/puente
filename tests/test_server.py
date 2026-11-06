"""Tests end-to-end: servidor real en un puerto local + cliente MCP por HTTP."""

import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from puente.server import create_app, parse_peers
from puente.store import RelayStore

ALICE, BOB = "a" * 32, "b" * 32


@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    store = RelayStore(str(tmp_path_factory.mktemp("db") / "relay.db"))
    store.init({"alice": ALICE, "bob": BOB})
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(create_app(store), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def client(base_url, token):
    return Client(
        StreamableHttpTransport(f"{base_url}/mcp", headers={"Authorization": f"Bearer {token}"})
    )


def test_healthz_sin_auth(base_url):
    r = httpx.get(f"{base_url}/healthz")
    assert r.status_code == 200 and r.text == "ok"


@pytest.mark.parametrize("auth", [None, "Bearer", "Bearer " + "z" * 32, "Basic abc"])
def test_mcp_rechaza_sin_token_valido(base_url, auth):
    headers = {"Authorization": auth} if auth else {}
    r = httpx.post(f"{base_url}/mcp", headers=headers, json={})
    assert r.status_code == 401


async def test_flujo_completo_entre_dos_peers(base_url):
    async with client(base_url, ALICE) as a, client(base_url, BOB) as b:
        quien = (await a.call_tool("relay_whoami", {})).data
        assert quien["yo"] == "alice" and set(quien["peers"]) == {"alice", "bob"}

        enviado = (await a.call_tool("relay_send", {"para": "bob", "mensaje": "¿listo?"})).data
        inbox = (await b.call_tool("relay_inbox", {})).structured_content["result"]
        assert [(m["de"], m["cuerpo"]) for m in inbox] == [("alice", "¿listo?")]

        await b.call_tool("relay_send", {"para": "alice", "mensaje": "sí", "hilo": enviado["hilo"]})
        hilo = (await a.call_tool("relay_thread", {"hilo": enviado["hilo"]})).structured_content
        assert [m["de"] for m in hilo["result"]] == ["alice", "bob"]


async def test_la_identidad_sale_del_token_no_de_los_argumentos(base_url):
    # relay_send no acepta 'de': un modelo no puede firmar como otro peer.
    async with client(base_url, BOB) as b:
        tools = {t.name: t for t in await b.list_tools()}
        assert "de" not in tools["relay_send"].inputSchema["properties"]
        await b.call_tool("relay_send", {"para": "alice", "mensaje": "firmado por bob"})
    async with client(base_url, ALICE) as a:
        inbox = (await a.call_tool("relay_inbox", {})).structured_content["result"]
        assert inbox[-1]["de"] == "bob"


def test_parse_peers_valido():
    assert parse_peers(f"alice:{ALICE}, bob:{BOB}") == {"alice": ALICE, "bob": BOB}


@pytest.mark.parametrize(
    "raw",
    [
        "",
        f"alice:{ALICE}",
        f"alice:{ALICE},bob:corto",
        f"alice:{ALICE},bob:{ALICE}",
        f"alice:{ALICE},alice:{BOB}",
        f"alice{ALICE},bob:{BOB}",
    ],
)
def test_parse_peers_invalido(raw):
    with pytest.raises(ValueError):
        parse_peers(raw)
