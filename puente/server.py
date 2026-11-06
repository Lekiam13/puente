"""Transporte: servidor MCP remoto (streamable HTTP) sobre RelayStore.

La identidad del llamador se deriva SIEMPRE del bearer token del request,
nunca de un argumento de la herramienta: un modelo manipulado por prompt
injection no puede hacerse pasar por otro peer.
"""

import os

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from puente.store import RelayStore

MIN_TOKEN = 32


def parse_peers(raw: str) -> dict[str, str]:
    """Lee 'nombre:token,nombre:token' y valida la configuración al arrancar."""
    peers: dict[str, str] = {}
    for item in filter(None, (p.strip() for p in raw.split(","))):
        nombre, sep, token = item.partition(":")
        nombre, token = nombre.strip(), token.strip()
        if not sep or not nombre or not token:
            raise ValueError(f"Entrada inválida en PUENTE_PEERS: {nombre or item!r}")
        if len(token) < MIN_TOKEN:
            raise ValueError(f"El token de {nombre!r} debe tener al menos {MIN_TOKEN} caracteres")
        if nombre in peers:
            raise ValueError(f"Peer duplicado: {nombre!r}")
        peers[nombre] = token
    if len(peers) < 2:
        raise ValueError("PUENTE_PEERS necesita al menos dos peers")
    if len(set(peers.values())) != len(peers):
        raise ValueError("Cada peer debe tener un token distinto")
    return peers


def bearer_token(auth: str) -> str | None:
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


class BearerAuthMiddleware:
    """ASGI: rechaza con 401 cualquier request a /mcp sin un token válido,
    antes de que llegue a la capa MCP. /healthz queda abierto."""

    def __init__(self, app, store: RelayStore):
        self.app = app
        self.store = store

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/mcp"):
            headers = dict(scope.get("headers") or [])
            token = bearer_token(headers.get(b"authorization", b"").decode("latin-1"))
            if not self.store.resolve_peer(token):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_mcp(store: RelayStore) -> FastMCP:
    mcp = FastMCP("puente")

    def caller() -> str:
        nombre = store.resolve_peer(bearer_token(get_http_headers().get("authorization", "")))
        if not nombre:
            raise PermissionError("Token inválido o ausente")
        return nombre

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    @mcp.tool()
    def relay_whoami() -> dict:
        """Quién soy (según mi token) y qué peers conoce el relay."""
        return {"yo": caller(), "peers": store.peers()}

    @mcp.tool()
    def relay_send(para: str, mensaje: str, hilo: str | None = None) -> dict:
        """Envía un mensaje a otro peer. Sin 'hilo' se abre un hilo nuevo."""
        return store.send(caller(), para, mensaje, hilo)

    @mcp.tool()
    def relay_inbox() -> list:
        """Mensajes no leídos dirigidos a mí. Quedan marcados como leídos."""
        return store.inbox(caller())

    @mcp.tool()
    def relay_thread(hilo: str) -> list:
        """Historial completo de un hilo, en orden. No cambia el estado de leído."""
        caller()
        return store.thread(hilo)

    @mcp.tool()
    def relay_context_push(resumen: str) -> dict:
        """Publica (o reemplaza) un bloque largo de contexto propio para los demás peers."""
        return store.context_push(caller(), resumen)

    @mcp.tool()
    def relay_context_pull(peer: str) -> dict:
        """Lee el último bloque de contexto que publicó otro peer."""
        caller()
        return store.context_pull(peer)

    return mcp


def create_app(store: RelayStore):
    app = create_mcp(store).http_app(path="/mcp")
    app.add_middleware(BearerAuthMiddleware, store=store)
    return app


def main() -> None:
    store = RelayStore(os.environ.get("PUENTE_DB", "/data/puente.db"))
    store.init(parse_peers(os.environ.get("PUENTE_PEERS", "")))
    uvicorn.run(create_app(store), host="0.0.0.0", port=int(os.environ.get("PORT", "8070")))


if __name__ == "__main__":
    main()
