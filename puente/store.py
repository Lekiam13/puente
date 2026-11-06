"""Capa de dominio y persistencia del relay.

No sabe nada de MCP ni de HTTP: recibe la identidad del llamador ya resuelta y
opera sobre SQLite. Así se puede testear sin levantar el servidor y cambiar el
transporte sin tocar la lógica.
"""

import hashlib
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS peers (
    token_hash TEXT PRIMARY KEY,
    nombre     TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS mensajes (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    hilo    TEXT NOT NULL,
    de      TEXT NOT NULL,
    para    TEXT NOT NULL,
    cuerpo  TEXT NOT NULL,
    creado  TEXT NOT NULL,
    leido   TEXT
);

CREATE INDEX IF NOT EXISTS idx_mensajes_para_leido ON mensajes(para, leido);
CREATE INDEX IF NOT EXISTS idx_mensajes_hilo ON mensajes(hilo);

CREATE TABLE IF NOT EXISTS contextos (
    peer        TEXT PRIMARY KEY,
    cuerpo      TEXT NOT NULL,
    actualizado TEXT NOT NULL
);
"""

MAX_MENSAJE = 64_000


class PeerDesconocido(ValueError):
    pass


def hash_token(token: str) -> str:
    """Los tokens nunca se guardan en claro: si alguien lee la base, no obtiene credenciales."""
    return hashlib.sha256(token.encode()).hexdigest()


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class RelayStore:
    def __init__(self, db_path: str):
        self.db_path = db_path

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:  # commit al salir, rollback si hay excepción
                yield conn
        finally:
            conn.close()

    def init(self, peers: dict[str, str]) -> None:
        """Crea el esquema y sincroniza los peers configurados (nombre -> token).

        Es idempotente: si un token rota, la fila vieja de ese peer se reemplaza
        y el token anterior deja de valer. Los peers que ya no están en la
        configuración se eliminan.
        """
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.executescript(SCHEMA)
            conn.execute(
                f"DELETE FROM peers WHERE nombre NOT IN ({','.join('?' * len(peers))})",
                list(peers),
            )
            for nombre, token in peers.items():
                conn.execute("DELETE FROM peers WHERE nombre = ?", (nombre,))
                conn.execute(
                    "INSERT INTO peers (token_hash, nombre) VALUES (?, ?)",
                    (hash_token(token), nombre),
                )

    def resolve_peer(self, token: str | None) -> str | None:
        if not token:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT nombre FROM peers WHERE token_hash = ?", (hash_token(token),)
            ).fetchone()
        return row["nombre"] if row else None

    def peers(self) -> list[str]:
        with self._conn() as conn:
            return [r["nombre"] for r in conn.execute("SELECT nombre FROM peers ORDER BY nombre")]

    def send(self, de: str, para: str, mensaje: str, hilo: str | None = None) -> dict:
        if not mensaje.strip():
            raise ValueError("El mensaje está vacío")
        if len(mensaje) > MAX_MENSAJE:
            raise ValueError(f"El mensaje supera {MAX_MENSAJE} caracteres")
        with self._conn() as conn:
            if not conn.execute("SELECT 1 FROM peers WHERE nombre = ?", (para,)).fetchone():
                raise PeerDesconocido(f"Peer desconocido: {para}")
            hilo_final = hilo or uuid.uuid4().hex[:8]
            cur = conn.execute(
                "INSERT INTO mensajes (hilo, de, para, cuerpo, creado) VALUES (?, ?, ?, ?, ?)",
                (hilo_final, de, para, mensaje, now_iso()),
            )
            return {"id": cur.lastrowid, "hilo": hilo_final}

    def inbox(self, yo: str) -> list[dict]:
        """Devuelve los no leídos y los marca leídos en UNA sentencia.

        Con SELECT + UPDATE por separado, dos lecturas concurrentes del mismo
        peer podían recibir el mismo mensaje dos veces. UPDATE ... RETURNING
        lo hace atómico: cada mensaje se entrega a lo más una vez.
        """
        with self._conn() as conn:
            rows = conn.execute(
                "UPDATE mensajes SET leido = ? WHERE para = ? AND leido IS NULL "
                "RETURNING id, de, hilo, cuerpo, creado",
                (now_iso(), yo),
            ).fetchall()
        return sorted((dict(r) for r in rows), key=lambda r: r["id"])

    def thread(self, hilo: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, de, para, cuerpo, creado, leido FROM mensajes "
                "WHERE hilo = ? ORDER BY id",
                (hilo,),
            ).fetchall()
        return [dict(r) for r in rows]

    def context_push(self, yo: str, resumen: str) -> dict:
        actualizado = now_iso()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO contextos (peer, cuerpo, actualizado) VALUES (?, ?, ?) "
                "ON CONFLICT(peer) DO UPDATE SET cuerpo = excluded.cuerpo, "
                "actualizado = excluded.actualizado",
                (yo, resumen, actualizado),
            )
        return {"peer": yo, "actualizado": actualizado}

    def context_pull(self, peer: str) -> dict:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT cuerpo, actualizado FROM contextos WHERE peer = ?", (peer,)
            ).fetchone()
        if not row:
            return {"peer": peer, "cuerpo": None, "actualizado": None}
        return {"peer": peer, "cuerpo": row["cuerpo"], "actualizado": row["actualizado"]}
