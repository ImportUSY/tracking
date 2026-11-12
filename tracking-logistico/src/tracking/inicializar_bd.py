"""Inicialización y limpieza de la base de datos (instalación de la versión final).

Reglas de negocio
-----------------
* SE VACÍAN (quedan en cero, con los contadores de id reiniciados):
  - Remesas y facturas, con todo lo que depende de ellas: historial de estatus (``eventos_estatus``),
    fotos de entrega/devolución (``fotos``), capturas de facturas por foto (``capturas_factura``) y
    cargas de Excel (``importaciones``).
  - Conductores (tabla ``conductores``) y camiones.
  - Catálogos: empresas de transporte (``empresas``) y ``vendedores``.
  - Historial: ``auditoria``, ``bitacora``, ``bitacora_archivos``, ``consultas_publicas`` y
    Datos de Navegación (``consumo_datos``); retos de huella pendientes (``retos_biometricos``).
  - Usuarios: se borran todos los que NO son Supervisor, Cobranzas o Consultor (conductores y
    transportistas), con sus llaves de huella.
* SE CONSERVAN: ``feriados`` (intactos) y los usuarios Supervisor, Cobranzas y Consultor con sus claves
  y llaves de huella; a estos solo se les quita la empresa asignada (las empresas se vacían).

Seguridad
---------
* Todo ocurre en UNA transacción. Antes de confirmarla se valida que las tablas a vaciar estén en
  cero y que las conservadas tengan exactamente el mismo contenido que antes (huella SHA-256 de cada
  fila). Si algo no cuadra se deshace todo y no se borra nada.
* Antes de borrar se guarda un respaldo completo (copia del archivo SQLite, o todas las tablas de
  PostgreSQL en JSON Lines comprimido). Las fotos guardadas en disco se mueven al respaldo, no se borran.
* ``--simular`` hace todo el proceso y las validaciones y al final lo deshace (no cambia nada).

Uso
---
    python -m tracking.inicializar_bd --destino local              (pregunta antes de borrar)
    python -m tracking.inicializar_bd --destino produccion --si     (desatendido)
    python -m tracking.inicializar_bd --destino local --simular

Códigos de salida: 0 = terminado, 1 = cancelado o error, 2 = la validación falló (no se cambió nada).
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import gzip
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import Connection, Engine, create_engine, insert, inspect, text

from tracking.enums import AccionAuditoria, Rol

Destino = Literal["local", "produccion"]

# Orden de borrado: primero los hijos (sirve para SQLite, que borra fila a fila con claves foráneas activas).
TABLAS_OPERATIVAS: tuple[str, ...] = (
    "eventos_estatus",
    "capturas_factura",
    "fotos",
    "facturas",
    "remesas",
    "importaciones",
    "conductores",
    "camiones",
    "vendedores",
    "auditoria",
    "consultas_publicas",
    "consumo_datos",
    "retos_biometricos",
    "bitacora",
    "bitacora_archivos",
    "bitacora_archivando",
)
# Las empresas se vacían al final: los usuarios que se conservan pueden apuntar a una
TABLAS_A_VACIAR: tuple[str, ...] = (*TABLAS_OPERATIVAS, "empresas")
# Tablas que deben quedar EXACTAMENTE igual (se comparan fila por fila con una huella SHA-256)
TABLAS_CONSERVADAS: tuple[str, ...] = ("feriados",)
ROLES_CONSERVADOS: tuple[str, ...] = (Rol.SUPERVISOR.value, Rol.COBRANZAS.value, Rol.CONSULTOR.value)
_EN_ROLES = "rol IN (" + ", ".join(f"'{r}'" for r in ROLES_CONSERVADOS) + ")"
# Columnas de los usuarios conservados que cambian a propósito: quién les cambió la clave (si fue un
# usuario borrado) y la empresa (las empresas se vacían)
_COLUMNA_USUARIO_VOLATIL = "clave_actualizada_por_id"
_COLUMNAS_USUARIO_VOLATILES = (_COLUMNA_USUARIO_VOLATIL, "empresa_id")
ACCION_AUDITORIA = AccionAuditoria.BASE_INICIALIZADA.value


class ValidacionError(RuntimeError):
    """La verificación posterior a la limpieza falló: la transacción se deshizo."""


@dataclass
class Resultado:
    destino: str
    dialecto: str
    simulacion: bool
    antes: dict[str, int]
    despues: dict[str, int]
    respaldo: Path | None = None
    fotos_movidas: int = 0
    fotos_respaldo: Path | None = None
    avisos: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------------------------- conexión
def _leer_env(archivo: Path) -> dict[str, str]:
    from dotenv import dotenv_values

    if not archivo.is_file():
        return {}
    return {k: (v or "").strip() for k, v in dotenv_values(archivo).items()}


def url_de_destino(destino: Destino, proyecto: Path) -> str:
    """Local: DATABASE_URL de ``.env`` (por defecto el SQLite de ``data/``). Producción: ``.env.local`` (Neon)."""
    if destino == "local":
        url = _leer_env(proyecto / ".env").get("DATABASE_URL") or "sqlite:///./data/tracking.db"
        ruta = url.split("sqlite:///", 1)[1] if url.startswith("sqlite:///") else ""
        es_absoluta = Path(ruta).is_absolute() or (len(ruta) > 1 and ruta[1] == ":")  # C:/… en Windows
        if ruta and ruta != ":memory:" and not es_absoluta:
            url = "sqlite:///" + str((proyecto / ruta).resolve()).replace("\\", "/")
        return url
    env = _leer_env(proyecto / ".env.local")
    for clave in ("DATABASE_URL_UNPOOLED", "POSTGRES_URL_NON_POOLING", "DATABASE_URL", "POSTGRES_URL"):
        if env.get(clave):
            return env[clave]
    raise SystemExit(
        "No se encontró la base de producción: falta DATABASE_URL en .env.local "
        "(se crea con «vercel env pull .env.local»)."
    )


def crear_motor(url: str) -> Engine:
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://") and "+psycopg" not in url.split("://", 1)[0]:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    if url.startswith("sqlite"):
        from sqlalchemy import event

        motor = create_engine(url, future=True)

        def _pragmas(dbapi_conn: Any, _r: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=10000")
            cur.close()

        event.listen(motor, "connect", _pragmas)
        return motor
    # Neon exige SSL; sin channel_binding a veces cierra el socket («server closed the connection»).
    from sqlalchemy.engine import make_url

    u = make_url(url)
    q = dict(u.query)
    if "neon.tech" in (u.host or ""):
        q.setdefault("sslmode", "require")
        q.setdefault("channel_binding", "require")
    url = u.set(query=q).render_as_string(hide_password=False)
    return create_engine(
        url, future=True, pool_pre_ping=True, connect_args={"connect_timeout": 8, "prepare_threshold": None}
    )


def despertar(motor: Engine, log: Callable[[str], None], intentos: int = 6) -> None:
    """Neon apaga el cómputo cuando no hay tráfico: el primer intento suele fallar. Se reintenta."""
    import time

    from sqlalchemy.exc import OperationalError

    ultimo: Exception | None = None
    for n in range(1, intentos + 1):
        try:
            with motor.connect() as conn:
                conn.execute(text("SELECT 1"))
            if n > 1:
                log(f"Conectado a la base en el intento {n}.")
            return
        except OperationalError as e:
            ultimo = e
            espera = min(8, 2 * n)
            log(f"Neon no respondió (intento {n} de {intentos}). Reintento en {espera} s…")
            time.sleep(espera)
    raise ultimo if ultimo else RuntimeError("No se pudo conectar")


def describir(url: str) -> str:
    """URL sin usuario ni clave, para mostrarla en pantalla y en el registro."""
    if url.startswith("sqlite"):
        return url
    from sqlalchemy.engine import make_url

    u = make_url(url.replace("postgres://", "postgresql://", 1))
    return f"PostgreSQL {u.host}/{u.database}"


# --------------------------------------------------------------------------------------------- utilidades
def _q(conn: Connection, nombre: str) -> str:
    return conn.dialect.identifier_preparer.quote(nombre)


def _tablas(conn: Connection) -> set[str]:
    return set(inspect(conn).get_table_names())


def conteos(conn: Connection) -> dict[str, int]:
    existentes = _tablas(conn)
    out: dict[str, int] = {}
    for t in (*TABLAS_A_VACIAR, *TABLAS_CONSERVADAS, "usuarios"):
        if t in existentes:
            out[t] = int(conn.execute(text(f"SELECT COUNT(*) FROM {_q(conn, t)}")).scalar_one())
    if "usuarios" in existentes:
        for rol in Rol:
            out[f"usuarios_{rol.value}"] = int(
                conn.execute(text("SELECT COUNT(*) FROM usuarios WHERE rol = :r"), {"r": rol.value}).scalar_one()
            )
        out["usuarios_conservados"] = int(
            conn.execute(text(f"SELECT COUNT(*) FROM usuarios WHERE {_EN_ROLES}")).scalar_one()
        )
        out["usuarios_borrar"] = out["usuarios"] - out["usuarios_conservados"]
    return out


def _valor(v: Any) -> Any:
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"b64": base64.b64encode(bytes(v)).decode()}
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    return v


def huella(
    conn: Connection, tabla: str, donde: str = "", params: dict[str, Any] | None = None, excluir: Iterable[str] = ()
) -> str:
    """SHA-256 del contenido completo de la tabla (ordenado por clave primaria)."""
    insp = inspect(conn)
    cols = [c["name"] for c in insp.get_columns(tabla) if c["name"] not in set(excluir)]
    pk = insp.get_pk_constraint(tabla).get("constrained_columns") or cols[:1]
    sql = (
        f"SELECT {', '.join(_q(conn, c) for c in cols)} FROM {_q(conn, tabla)} {donde} "
        f"ORDER BY {', '.join(_q(conn, c) for c in pk)}"
    )
    h = hashlib.sha256()
    for fila in conn.execute(text(sql), params or {}):
        h.update(json.dumps([_valor(v) for v in fila], default=str, ensure_ascii=False).encode())
        h.update(b"\n")
    return h.hexdigest()


def huellas_conservadas(conn: Connection) -> dict[str, str]:
    existentes = _tablas(conn)
    out = {t: huella(conn, t) for t in TABLAS_CONSERVADAS if t in existentes}
    out["usuarios_conservados"] = huella(conn, "usuarios", f"WHERE {_EN_ROLES}", excluir=_COLUMNAS_USUARIO_VOLATILES)
    return out


def verificar_esquema(motor: Engine, actualizar: bool, log: Callable[[str], None]) -> None:
    """La base debe estar en la última migración (si no, faltarían tablas o columnas)."""
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    from tracking.migrate import alembic_config, upgrade_head

    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    with motor.connect() as conn:
        actual = MigrationContext.configure(conn).get_current_revision()
    if actual == head:
        return
    if not actualizar:
        raise SystemExit(
            f"La base está en la migración {actual} y el sistema en {head}. Publique la versión "
            "actual (la aplicación migra sola al arrancar) y vuelva a ejecutar la limpieza."
        )
    log(f"Actualizando el esquema de la base: {actual} → {head}")
    upgrade_head(motor)


# --------------------------------------------------------------------------------------------- respaldo
def respaldar(motor: Engine, url: str, carpeta: Path, sello: str, destino: str, log: Callable[[str], None]) -> Path:
    carpeta.mkdir(parents=True, exist_ok=True)
    if url.startswith("sqlite"):
        origen = url.split("sqlite:///", 1)[1]
        archivo = carpeta / f"{destino}_{sello}.db"
        src, dst = sqlite3.connect(origen), sqlite3.connect(archivo)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        log(f"Respaldo completo de la base: {archivo}")
        return archivo
    archivo = carpeta / f"{destino}_{sello}.jsonl.gz"
    total = 0
    with motor.connect() as conn, gzip.open(archivo, "wt", encoding="utf-8") as gz:
        insp = inspect(conn)
        for tabla in sorted(insp.get_table_names()):
            cols = [c["name"] for c in insp.get_columns(tabla)]
            res = conn.execution_options(stream_results=True, yield_per=500).execute(
                text(f"SELECT {', '.join(_q(conn, c) for c in cols)} FROM {_q(conn, tabla)}")
            )
            n = 0
            for fila in res:
                gz.write(
                    json.dumps(
                        {"tabla": tabla, "fila": {c: _valor(v) for c, v in zip(cols, fila, strict=True)}},
                        ensure_ascii=False,
                        default=str,
                    )
                    + "\n"
                )
                n += 1
            gz.write(json.dumps({"tabla": tabla, "filas": n, "fin": True}) + "\n")
            total += n
    log(f"Respaldo completo de la base ({total} filas de todas las tablas): {archivo}")
    return archivo


# --------------------------------------------------------------------------------------------- limpieza
def _rutas_en_disco(conn: Connection) -> list[str]:
    rutas: list[str] = []
    existentes = _tablas(conn)
    for t in ("fotos", "capturas_factura"):
        if t in existentes:
            rutas += [
                r
                for (r,) in conn.execute(
                    text(f"SELECT ruta FROM {t} WHERE ruta IS NOT NULL AND ruta <> 'db' AND url_externa IS NULL")
                )
            ]
    return rutas


def _vaciar_postgres(conn: Connection, existentes: set[str]) -> None:
    conn.execute(text("SET LOCAL lock_timeout = '30s'"))
    tablas = [t for t in TABLAS_OPERATIVAS if t in existentes]
    # TRUNCATE no dispara los disparadores fila a fila que hacen inmutable la bitácora, y reinicia los
    # contadores (RESTART IDENTITY). Sin CASCADE: si alguna otra tabla dependiera de estas, falla y no borra.
    conn.execute(text(f"TRUNCATE TABLE {', '.join(_q(conn, t) for t in tablas)} RESTART IDENTITY"))


def _vaciar_sqlite(conn: Connection, existentes: set[str]) -> None:
    # Romper referencias circulares (foto de evidencia y reingresos) antes de borrar
    if "facturas" in existentes:
        conn.execute(text("UPDATE facturas SET evidencia_foto_id = NULL, reingreso_de_id = NULL"))
    # Los disparadores que impiden borrar el ARCHIVO de la bitácora se quitan y se reponen
    # dentro de la misma transacción (si algo falla, vuelven solos con el ROLLBACK).
    disparadores = [
        (n, s)
        for n, s in conn.execute(
            text("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'bitacora_archivos'")
        )
    ]
    for nombre, _ in disparadores:
        conn.execute(text(f'DROP TRIGGER "{nombre}"'))
    for t in TABLAS_OPERATIVAS:
        if t not in existentes or t == "bitacora_archivando":
            continue
        if t == "bitacora" and "bitacora_archivando" in existentes:
            # Permiso de borrado de la bitácora (mismo mecanismo que usa el archivado)
            conn.execute(text("INSERT INTO bitacora_archivando (id) VALUES (1)"))
            conn.execute(text("DELETE FROM bitacora"))
            conn.execute(text("DELETE FROM bitacora_archivando"))
            continue
        conn.execute(text(f'DELETE FROM "{t}"'))
    for _, sql in disparadores:
        conn.execute(text(sql))


def _reiniciar_contadores_sqlite(conn: Connection) -> None:
    if conn.execute(text("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'")).first():
        conn.execute(
            text("DELETE FROM sqlite_sequence WHERE name IN (" + ", ".join(f"'{t}'" for t in TABLAS_A_VACIAR) + ")")
        )


def _borrar_usuarios(conn: Connection) -> None:
    """Quedan solo Supervisor, Cobranzas y Consultor (sin empresa asignada); se borran sus llaves de huella
    a los demás."""
    ids = f"SELECT id FROM usuarios WHERE NOT ({_EN_ROLES})"
    conn.execute(
        text(f"UPDATE usuarios SET {_COLUMNA_USUARIO_VOLATIL} = NULL WHERE {_COLUMNA_USUARIO_VOLATIL} IN ({ids})")
    )
    if "credenciales_biometricas" in _tablas(conn):
        conn.execute(text(f"DELETE FROM credenciales_biometricas WHERE usuario_id IN ({ids})"))
    conn.execute(text(f"DELETE FROM usuarios WHERE NOT ({_EN_ROLES})"))
    conn.execute(text("UPDATE usuarios SET empresa_id = NULL WHERE empresa_id IS NOT NULL"))


def _vaciar_empresas(conn: Connection) -> None:
    conn.execute(text("DELETE FROM empresas"))
    if conn.dialect.name == "postgresql":
        conn.execute(text("SELECT setval(pg_get_serial_sequence('empresas', 'id'), 1, false)"))


def validar(conn: Connection, antes: dict[str, int], huellas_antes: dict[str, str]) -> list[str]:
    """Reglas que deben cumplirse antes de confirmar. Devuelve la lista de errores (vacía = todo bien)."""
    errores: list[str] = []
    despues = conteos(conn)
    for t in TABLAS_A_VACIAR:
        if despues.get(t, 0) != 0:
            errores.append(f"La tabla {t} debía quedar vacía y tiene {despues[t]} registro(s).")
    if despues.get("usuarios_borrar", 0) != 0:
        errores.append(f"Quedan {despues['usuarios_borrar']} usuario(s) que no son Supervisor, Cobranzas o Consultor.")
    if not despues.get(f"usuarios_{Rol.SUPERVISOR.value}"):
        errores.append("No quedaría ningún usuario Supervisor: nadie podría administrar el sistema.")
    con_empresa = conn.execute(text("SELECT COUNT(*) FROM usuarios WHERE empresa_id IS NOT NULL")).scalar_one()
    if con_empresa:
        errores.append(f"{con_empresa} usuario(s) conservan una empresa asignada.")
    if "credenciales_biometricas" in _tablas(conn):
        sueltas = conn.execute(text(
            "SELECT COUNT(*) FROM credenciales_biometricas c "
            "WHERE NOT EXISTS (SELECT 1 FROM usuarios u WHERE u.id = c.usuario_id)"
        )).scalar_one()
        if sueltas:
            errores.append(f"{sueltas} llave(s) de huella quedaron sin usuario.")
    for t in (*TABLAS_CONSERVADAS, "usuarios_conservados"):
        if t in antes and despues.get(t) != antes[t]:
            errores.append(f"{t}: había {antes[t]} registro(s) y ahora hay {despues.get(t)}; no se debía tocar.")
    huellas_despues = huellas_conservadas(conn)
    for t, h in huellas_antes.items():
        if huellas_despues.get(t) != h:
            errores.append(f"El contenido de {t} cambió; debía quedar intacto.")
    huerfanos = conn.execute(
        text(
            "SELECT COUNT(*) FROM usuarios u WHERE u.empresa_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM empresas e WHERE e.id = u.empresa_id)"
        )
    ).scalar_one()
    if huerfanos:
        errores.append(f"{huerfanos} usuario(s) quedaron apuntando a una empresa inexistente.")
    return errores


def _registrar_auditoria(conn: Connection, destino: str, antes: dict[str, int], respaldo: Path | None) -> None:
    from tracking.models import Auditoria

    borrados = {t: antes[t] for t in (*TABLAS_A_VACIAR, "usuarios_conductor", "usuarios_transportista")
                if antes.get(t)}
    conn.execute(
        insert(Auditoria).values(
            created_at=datetime.now(timezone.utc),
            actor_id=None,
            actor_rol="sistema",
            accion=ACCION_AUDITORIA,
            objetivo_usuario_id=None,
            detalle={
                "destino": destino,
                "borrados": borrados,
                "respaldo": respaldo.name if respaldo else None,
                "conservados": {t: antes.get(t, 0) for t in (*TABLAS_CONSERVADAS, "usuarios_conservados")},
            },
        )
    )


def limpiar(
    motor: Engine,
    *,
    destino: str,
    simular: bool = False,
    respaldo: Path | None = None,
    log: Callable[[str], None] = print,
) -> tuple[Resultado, list[str]]:
    """Ejecuta la limpieza en una sola transacción. Devuelve el resultado y las rutas de fotos en disco borradas."""
    with motor.connect() as conn:
        trans = conn.begin()
        try:
            existentes = _tablas(conn)
            faltan = {"usuarios", "empresas", "vendedores", "remesas", "facturas", "conductores"} - existentes
            if faltan:
                raise SystemExit(f"La base no tiene las tablas del sistema ({', '.join(sorted(faltan))}).")
            antes = conteos(conn)
            huellas_antes = huellas_conservadas(conn)
            rutas = _rutas_en_disco(conn)
            log("Vaciando remesas, facturas, conductores, camiones, vendedores e historial…")
            if conn.dialect.name == "postgresql":
                _vaciar_postgres(conn, existentes)
            else:
                _vaciar_sqlite(conn, existentes)
            log("Borrando usuarios (quedan Supervisor, Cobranzas y Consultor) y empresas…")
            _borrar_usuarios(conn)
            _vaciar_empresas(conn)
            if conn.dialect.name == "sqlite":
                _reiniciar_contadores_sqlite(conn)
            log("Validando que solo se borró lo permitido…")
            errores = validar(conn, antes, huellas_antes)
            if errores:
                trans.rollback()
                raise ValidacionError("\n".join(errores))
            despues = conteos(conn)
            if simular:
                trans.rollback()
                log("SIMULACIÓN: todo cuadró y se deshizo; la base no cambió.")
            else:
                _registrar_auditoria(conn, destino, antes, respaldo)
                trans.commit()
        except BaseException:
            if trans.is_active:
                trans.rollback()
            raise
    res = Resultado(
        destino=destino,
        dialecto=motor.dialect.name,
        simulacion=simular,
        antes=antes,
        despues=despues,
        respaldo=respaldo,
    )
    return res, rutas


def mover_fotos(rutas: list[str], carpeta_fotos: Path, destino: Path) -> int:
    """Las fotos guardadas en disco (modo local) se MUEVEN a la carpeta del respaldo."""
    n = 0
    base = carpeta_fotos.resolve()
    for rel in rutas:
        origen = (base / rel).resolve()
        if base not in origen.parents or not origen.is_file():
            continue
        final = destino / rel
        final.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(origen), str(final))
        n += 1
    # Carpetas que quedaron vacías
    for d in sorted((p for p in base.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        with contextlib.suppress(OSError):
            d.rmdir()
    return n


def url_https(proyecto: Path) -> str:
    """Conexión con la que Neon acepta consultas por HTTPS (puerto 443), no por el puerto 5432."""
    env = _leer_env(proyecto / ".env.local")
    for clave in ("DATABASE_URL", "POSTGRES_URL", "DATABASE_URL_UNPOOLED", "POSTGRES_URL_NON_POOLING"):
        if env.get(clave):
            return env[clave]
    raise SystemExit("No se encontró la base de producción en .env.local.")


def _api_neon(url: str) -> str:
    from sqlalchemy.engine import make_url

    host = make_url(url).host or ""
    if "neon.tech" not in host or "." not in host:
        raise RuntimeError("La dirección no es de Neon.")
    return "https://api." + host.split(".", 1)[1] + "/sql"


def neon_consultar(url: str, consultas: list[str]) -> list[list[dict[str, Any]]]:
    """Varias consultas en una sola transacción, por HTTPS. Si una falla, Neon deshace todas."""
    import urllib.error
    import urllib.request

    cuerpo = json.dumps({"queries": [{"query": q, "params": []} for q in consultas]}).encode()
    req = urllib.request.Request(
        _api_neon(url),
        data=cuerpo,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Neon-Connection-String": url,
            "Neon-Raw-Text-Output": "true",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detalle = exc.read().decode(errors="replace")[:400]
        raise RuntimeError(f"Neon respondió {exc.code}: {detalle}") from exc
    return [list(r.get("rows") or []) for r in data["results"]]


def sql_limpieza_postgres(tablas: set[str]) -> str:
    """Un solo bloque: vacía lo operativo, deja feriados y Supervisor, Cobranzas y Consultor, y valida."""
    truncar = [t for t in TABLAS_OPERATIVAS if t in tablas and re.fullmatch(r"[a-z_]+", t)]
    if not truncar:
        raise RuntimeError("No están las tablas del sistema.")
    lista = ", ".join(truncar)
    revisiones = "\n".join(
        f"  IF (SELECT count(*) FROM {t}) <> 0 THEN RAISE EXCEPTION 'la tabla {t} no quedo vacia'; END IF;"
        for t in (*truncar, "empresas")
        if t in tablas or t == "empresas"
    )
    cred = ""
    if "credenciales_biometricas" in tablas:
        cred = (
            "  DELETE FROM credenciales_biometricas WHERE usuario_id IN "
            f"(SELECT id FROM usuarios WHERE NOT ({_EN_ROLES}));\n"
        )
    return f"""
DO $limpieza$
DECLARE
  feriados_antes int;
  oficina_antes int;
BEGIN
  SELECT count(*) INTO feriados_antes FROM feriados;
  SELECT count(*) INTO oficina_antes FROM usuarios WHERE {_EN_ROLES};
  TRUNCATE TABLE {lista} RESTART IDENTITY;
  UPDATE usuarios SET {_COLUMNA_USUARIO_VOLATIL} = NULL
    WHERE {_COLUMNA_USUARIO_VOLATIL} IN (SELECT id FROM usuarios WHERE NOT ({_EN_ROLES}));
{cred}  DELETE FROM usuarios WHERE NOT ({_EN_ROLES});
  UPDATE usuarios SET empresa_id = NULL WHERE empresa_id IS NOT NULL;
  DELETE FROM empresas;
  PERFORM setval(pg_get_serial_sequence('empresas', 'id'), 1, false);
{revisiones}
  IF (SELECT count(*) FROM usuarios WHERE NOT ({_EN_ROLES})) <> 0 THEN
    RAISE EXCEPTION 'quedaron usuarios que no son Supervisor, Cobranzas o Consultor';
  END IF;
  IF (SELECT count(*) FROM usuarios WHERE rol = 'supervisor') = 0 THEN
    RAISE EXCEPTION 'no quedaria ningun Supervisor';
  END IF;
  IF (SELECT count(*) FROM usuarios WHERE empresa_id IS NOT NULL) <> 0 THEN
    RAISE EXCEPTION 'un usuario conserva empresa';
  END IF;
  IF (SELECT count(*) FROM feriados) <> feriados_antes THEN
    RAISE EXCEPTION 'cambiaron los feriados';
  END IF;
  IF (SELECT count(*) FROM usuarios WHERE {_EN_ROLES}) <> oficina_antes THEN
    RAISE EXCEPTION 'cambio la cantidad de usuarios de oficina';
  END IF;
  INSERT INTO auditoria (created_at, actor_id, actor_rol, accion, objetivo_usuario_id, detalle)
  VALUES (now(), NULL, 'sistema', '{ACCION_AUDITORIA}', NULL,
          jsonb_build_object('destino', 'produccion', 'via', 'https'));
END
$limpieza$;
"""


def _conteos_https(url: str, tablas: set[str]) -> dict[str, int]:
    nombres = [t for t in (*TABLAS_A_VACIAR, *TABLAS_CONSERVADAS, "usuarios") if t in tablas]
    consultas = [f"SELECT count(*)::int AS n FROM {t}" for t in nombres]
    consultas.append("SELECT rol, count(*)::int AS n FROM usuarios GROUP BY rol")
    filas = neon_consultar(url, consultas)
    out = {t: int(filas[i][0]["n"]) for i, t in enumerate(nombres)}
    roles = {r["rol"]: int(r["n"]) for r in filas[-1]}
    for rol in Rol:
        out[f"usuarios_{rol.value}"] = roles.get(rol.value, 0)
    out["usuarios_conservados"] = sum(out.get(f"usuarios_{r}", 0) for r in ROLES_CONSERVADOS)
    out["usuarios_borrar"] = out.get("usuarios", 0) - out["usuarios_conservados"]
    return out


def respaldar_https(url: str, tablas: set[str], carpeta: Path, sello: str, log: Callable[[str], None]) -> Path:
    """Respaldo por HTTPS. No incluye el binario de las fotos (columna bytea): el puerto 5432 no deja bajarlo."""
    carpeta.mkdir(parents=True, exist_ok=True)
    archivo = carpeta / f"produccion_{sello}.jsonl.gz"
    cols = neon_consultar(
        url,
        [
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND udt_name <> 'bytea' ORDER BY table_name, ordinal_position"
        ],
    )[0]
    por_tabla: dict[str, list[str]] = {}
    for c in cols:
        if c["table_name"] in tablas and re.fullmatch(r"[a-z_]+", c["column_name"]):
            por_tabla.setdefault(c["table_name"], []).append(c["column_name"])
    total = 0
    with gzip.open(archivo, "wt", encoding="utf-8") as gz:
        for tabla, columnas in sorted(por_tabla.items()):
            filas = neon_consultar(url, [f"SELECT {', '.join(columnas)} FROM {tabla}"])[0]
            for fila in filas:
                gz.write(json.dumps({"tabla": tabla, "fila": fila}, ensure_ascii=False, default=str) + "\n")
            gz.write(json.dumps({"tabla": tabla, "filas": len(filas), "fin": True}) + "\n")
            total += len(filas)
    log(f"Respaldo por HTTPS ({total} filas, sin el binario de las fotos): {archivo}")
    return archivo


def inicializar_por_https(
    proyecto: Path, *, simular: bool, confirmar: bool, carpeta: Path, sello: str, log: Callable[[str], None]
) -> int:
    url = url_https(proyecto)
    log(f"Base de datos: PRODUCCIÓN por HTTPS · {describir(url)}")
    nombres = {
        r["table_name"]
        for r in neon_consultar(
            url,
            ["SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"],
        )[0]
    }
    faltan = {"usuarios", "empresas", "feriados", "facturas", "remesas"} - nombres
    if faltan:
        raise SystemExit(f"La base no tiene las tablas del sistema ({', '.join(sorted(faltan))}).")
    antes = _conteos_https(url, nombres)
    log("Situación actual:\n" + tabla_resumen(antes, None))
    if not simular and not confirmar:
        print("\nEsto borra definitivamente lo marcado «SE VACÍA». Para continuar escriba BORRAR PRODUCCION: ", end="")
        if input().strip().upper() != "BORRAR PRODUCCION":
            log("Cancelado por el usuario: no se cambió nada.")
            return 1
    if simular:
        log("SIMULACIÓN por HTTPS: no se envió ninguna orden de borrado. La base no cambió.")
        log("Resultado:\n" + tabla_resumen(antes, antes))
        return 0
    respaldo = respaldar_https(url, nombres, carpeta, sello, log)
    log("Vaciando la base (una sola transacción; si algo no cuadra, Neon la deshace)…")
    neon_consultar(url, [sql_limpieza_postgres(nombres)])
    despues = _conteos_https(url, nombres)
    log(f"Respaldo: {respaldo.name}")
    log("Resultado:\n" + tabla_resumen(antes, despues))
    if despues.get("facturas") or despues.get("usuarios_borrar") or not despues.get("usuarios_supervisor"):
        log("VALIDACIÓN FALLIDA después de confirmar. Revise el respaldo.")
        return 2
    log("BASE INICIALIZADA CORRECTAMENTE.")
    return 0


def carpeta_fotos_local(proyecto: Path) -> Path | None:
    env = _leer_env(proyecto / ".env")
    if (env.get("STORAGE_BACKEND") or "disk").lower() == "db":
        return None
    d = Path(env.get("STORAGE_DIR") or "./data/fotos")
    return d if d.is_absolute() else (proyecto / d)


# --------------------------------------------------------------------------------------------- CLI
ETIQUETAS = {
    "remesas": "Remesas",
    "facturas": "Facturas",
    "eventos_estatus": "Historial de estatus",
    "fotos": "Fotos",
    "capturas_factura": "Capturas de facturas (foto)",
    "importaciones": "Cargas de Excel",
    "conductores": "Conductores",
    "usuarios_conductor": "Usuarios Conductor",
    "usuarios_transportista": "Usuarios Transportista",
    "camiones": "Camiones",
    "empresas": "Empresas de transporte",
    "vendedores": "Vendedores",
    "auditoria": "Auditoría de seguridad",
    "bitacora": "Bitácora",
    "bitacora_archivos": "Bitácora archivada",
    "consultas_publicas": "Consultas públicas",
    "consumo_datos": "Datos de Navegación",
    "feriados": "Feriados",
    "usuarios_supervisor": "Usuarios Supervisor",
    "usuarios_cobranzas": "Usuarios Cobranzas",
    "usuarios_consultor": "Usuarios Consultor",
}
_BORRA = (
    "remesas",
    "facturas",
    "eventos_estatus",
    "fotos",
    "capturas_factura",
    "importaciones",
    "conductores",
    "usuarios_conductor",
    "usuarios_transportista",
    "camiones",
    "empresas",
    "vendedores",
    "auditoria",
    "bitacora",
    "bitacora_archivos",
    "consultas_publicas",
    "consumo_datos",
)
_CONSERVA = ("feriados", "usuarios_supervisor", "usuarios_cobranzas", "usuarios_consultor")


def tabla_resumen(antes: dict[str, int], despues: dict[str, int] | None) -> str:
    lineas = [f"  {'':34}{'Antes':>8}{'Después':>10}"]
    for titulo, claves in (("SE VACÍA", _BORRA), ("SE CONSERVA", _CONSERVA)):
        lineas.append(f"  [{titulo}]")
        for k in claves:
            if k in antes:
                d = "" if despues is None else str(despues.get(k, 0))
                lineas.append(f"  {ETIQUETAS[k]:<34}{antes[k]:>8}{d:>10}")
    return "\n".join(lineas)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m tracking.inicializar_bd",
        description="Vacía todo (remesas, facturas, conductores, camiones, empresas, vendedores e historial); "
        "conserva los feriados y los usuarios Supervisor, Cobranzas y Consultor.",
    )
    p.add_argument("--destino", choices=("local", "produccion"), required=True)
    p.add_argument("--proyecto", type=Path, default=Path.cwd(), help="Carpeta tracking-logistico")
    p.add_argument(
        "--respaldos", type=Path, default=None, help="Carpeta de respaldos y registros (por defecto ..\\respaldos_bd)"
    )
    p.add_argument("--si", action="store_true", help="No preguntar (modo desatendido)")
    p.add_argument("--simular", action="store_true", help="Hacer todo y deshacerlo al final (no cambia nada)")
    a = p.parse_args(argv)

    proyecto = a.proyecto.resolve()
    carpeta = (a.respaldos or proyecto.parent / "respaldos_bd").resolve()
    carpeta.mkdir(parents=True, exist_ok=True)
    sello = datetime.now().strftime("%Y%m%d_%H%M%S")
    registro = carpeta / f"inicializacion_{a.destino}_{sello}{'_simulacion' if a.simular else ''}.log"

    def log(m: str) -> None:
        print(m, flush=True)
        with registro.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {m}\n")

    try:
        url = url_de_destino(a.destino, proyecto)
        log(f"Base de datos: {'PRODUCCIÓN' if a.destino == 'produccion' else 'LOCAL'} · {describir(url)}")
        motor = crear_motor(url)
        if a.destino == "produccion":
            log("Conectando a Neon…")
            try:
                despertar(motor, log, intentos=1)
            except Exception as exc:
                from sqlalchemy.exc import OperationalError

                if not isinstance(exc, OperationalError):
                    raise
                log(
                    "El puerto de Postgres (5432) no responde desde esta red. "
                    "El sitio publicado sí entra. Se continúa por HTTPS, que es el camino que esta red deja pasar."
                )
                motor.dispose()
                return inicializar_por_https(
                    proyecto, simular=a.simular, confirmar=a.si, carpeta=carpeta, sello=sello, log=log
                )
        verificar_esquema(motor, actualizar=a.destino == "local", log=log)
        with motor.connect() as conn:
            antes = conteos(conn)
        log("Situación actual:\n" + tabla_resumen(antes, None))
        if not a.simular and not a.si:
            clave = "BORRAR PRODUCCION" if a.destino == "produccion" else "BORRAR"
            print(f"\nEsto borra definitivamente lo marcado «SE VACÍA». Para continuar escriba {clave}: ", end="")
            if input().strip().upper() != clave:
                log("Cancelado por el usuario: no se cambió nada.")
                return 1
        respaldo = None if a.simular else respaldar(motor, url, carpeta, sello, a.destino, log)
        res, rutas = limpiar(motor, destino=a.destino, simular=a.simular, respaldo=respaldo, log=log)
        if not a.simular and rutas and a.destino == "local":
            fotos = carpeta_fotos_local(proyecto)
            if fotos and fotos.is_dir():
                res.fotos_respaldo = carpeta / f"{a.destino}_{sello}_fotos"
                res.fotos_movidas = mover_fotos(rutas, fotos, res.fotos_respaldo)
                log(f"{res.fotos_movidas} foto(s) en disco movidas al respaldo: {res.fotos_respaldo}")
        log("Resultado:\n" + tabla_resumen(res.antes, res.despues))
        log("SIMULACIÓN TERMINADA (sin cambios)." if a.simular else "BASE INICIALIZADA CORRECTAMENTE.")
        motor.dispose()
        return 0
    except ValidacionError as e:
        log(f"VALIDACIÓN FALLIDA, se deshizo todo y la base no cambió:\n{e}")
        return 2
    except SystemExit as e:
        if isinstance(e.code, str):
            log(f"ERROR: {e.code}")
            return 1
        raise
    except Exception as e:  # cualquier error se informa y se registra
        log(f"ERROR: {type(e).__name__}: {e}. La transacción se deshizo; no se borró nada.")
        return 1
    finally:
        print(f"\nRegistro: {registro}")


if __name__ == "__main__":
    sys.exit(main())
