import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from puente.store import PeerDesconocido, RelayStore, hash_token

TOKENS = {"alice": "a" * 32, "bob": "b" * 32}


@pytest.fixture
def store(tmp_path):
    s = RelayStore(str(tmp_path / "relay.db"))
    s.init(TOKENS)
    return s


def test_resuelve_peer_por_token(store):
    assert store.resolve_peer("a" * 32) == "alice"
    assert store.resolve_peer("x" * 32) is None
    assert store.resolve_peer(None) is None


def test_tokens_no_se_guardan_en_claro(store):
    conn = sqlite3.connect(store.db_path)
    guardados = {r[0] for r in conn.execute("SELECT token_hash FROM peers")}
    assert guardados == {hash_token(t) for t in TOKENS.values()}
    assert not guardados & set(TOKENS.values())


def test_rotar_token_invalida_el_anterior(store):
    store.init({"alice": "n" * 32, "bob": "b" * 32})
    assert store.resolve_peer("n" * 32) == "alice"
    assert store.resolve_peer("a" * 32) is None


def test_peer_quitado_de_la_config_pierde_acceso(store):
    store.init({"alice": "a" * 32, "carol": "c" * 32})
    assert store.resolve_peer("b" * 32) is None
    assert store.peers() == ["alice", "carol"]


def test_send_e_inbox(store):
    r = store.send("alice", "bob", "hola")
    assert r["id"] and len(r["hilo"]) == 8
    inbox = store.inbox("bob")
    assert [(m["de"], m["cuerpo"], m["hilo"]) for m in inbox] == [("alice", "hola", r["hilo"])]


def test_inbox_entrega_una_sola_vez(store):
    store.send("alice", "bob", "uno")
    assert len(store.inbox("bob")) == 1
    assert store.inbox("bob") == []


def test_inbox_solo_ve_lo_propio(store):
    store.send("alice", "bob", "para bob")
    assert store.inbox("alice") == []


def test_inbox_concurrente_no_duplica(store):
    for i in range(200):
        store.send("alice", "bob", f"m{i}")
    with ThreadPoolExecutor(max_workers=8) as pool:
        lotes = list(pool.map(lambda _: store.inbox("bob"), range(8)))
    ids = [m["id"] for lote in lotes for m in lote]
    assert len(ids) == 200
    assert len(set(ids)) == 200


def test_send_a_peer_desconocido(store):
    with pytest.raises(PeerDesconocido):
        store.send("alice", "mallory", "hola")


@pytest.mark.parametrize("mensaje", ["", "   ", "x" * 64_001])
def test_send_rechaza_mensajes_invalidos(store, mensaje):
    with pytest.raises(ValueError):
        store.send("alice", "bob", mensaje)


def test_thread_ordenado_y_no_marca_leido(store):
    hilo = store.send("alice", "bob", "pregunta")["hilo"]
    store.send("bob", "alice", "respuesta", hilo=hilo)
    hist = store.thread(hilo)
    assert [m["cuerpo"] for m in hist] == ["pregunta", "respuesta"]
    assert all(m["leido"] is None for m in hist)
    assert len(store.inbox("bob")) == 1


def test_context_push_reemplaza(store):
    assert store.context_pull("alice")["cuerpo"] is None
    store.context_push("alice", "v1")
    store.context_push("alice", "v2")
    assert store.context_pull("alice")["cuerpo"] == "v2"
