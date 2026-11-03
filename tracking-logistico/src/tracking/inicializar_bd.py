"""Inicialización y limpieza selectiva de la base de datos (reseteo operativo).

Reglas de negocio
-----------------
* SE VACÍAN (quedan en cero, con los contadores de id reiniciados):
  - Remesas y facturas, con todo lo que depende de ellas: historial de estatus (``eventos_estatus``),
    fotos de entrega/devolución (``fotos``), capturas de facturas por foto (``capturas_factura``) y
    cargas de Excel (``importaciones``).
  - Conductores: la tabla ``conductores`` y todos los usuarios con **rol conductor**.
  - Camiones (se vuelven a cargar con la hoja PLACAS junto con los conductores).
  - Historial: ``auditoria``, ``bitacora``, ``bitacora_archivos`` y ``consultas_publicas``.
* SE CONSERVAN INTACTOS: ``empresas``, ``vendedores``, ``feriados`` y los usuarios de oficina
  (supervisor, transportista, cobranzas y consultor) con sus claves.

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
TABLAS_A_VACIAR: tuple[str, ...] = (
    "eventos_estatus",
    "capturas_factura",
    "fotos",
    "facturas",
    "remesas",
    "importaciones",
    "conductores",
    "camiones",
    "auditoria",
    "consultas_publicas",
    "bitacora",
    "bitacora_archivos",
    "bitacora_archivando",
)
# Tablas que deben quedar EXACTAMENTE igual (se comparan fila por fila con una huella SHA-256)
TABLAS_CONSERVADAS: tuple[str, ...] = ("empresas", "vendedores", "feriados")
ROL_CONDUCTOR = Rol.CONDUCTOR.value
# Columna de usuarios que puede cambiar al borrar conductores (quién le cambió la clave por última vez)
_COLUMNA_USUARIO_VOLATIL = "clave_actualizada_por_id"
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
    if url.startswith("postgresql://"):
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
    return create_engine(
        url, future=True, pool_pre_ping=True, connect_args={"connect_timeout": 20, "prepare_threshold": None}
    )


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
        out["usuarios_conductor"] = int(
            conn.execute(text("SELECT COUNT(*) FROM usuarios WHERE rol = :r"), {"r": ROL_CONDUCTOR}).scalar_one()
        )
        out["usuarios_oficina"] = out["usuarios"] - out["usuarios_conductor"]
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
    out["usuarios_oficina"] = huella(
        conn, "usuarios", "WHERE rol <> :r", {"r": ROL_CONDUCTOR}, excluir=(_COLUMNA_USUARIO_VOLATIL,)
    )
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
    tablas = [t for t in TABLAS_A_VACIAR if t in existentes]
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
    for t in TABLAS_A_VACIAR:
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
    if (
        "sqlite_sequence" in existentes
        or conn.execute(text("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'")).first()
    ):
        conn.execute(
            text("DELETE FROM sqlite_sequence WHERE name IN (" + ", ".join(f"'{t}'" for t in TABLAS_A_VACIAR) + ")")
        )


def _borrar_usuarios_conductores(conn: Connection) -> None:
    ids = "SELECT id FROM usuarios WHERE rol = :r"
    conn.execute(
        text(f"UPDATE usuarios SET {_COLUMNA_USUARIO_VOLATIL} = NULL WHERE {_COLUMNA_USUARIO_VOLATIL} IN ({ids})"),
        {"r": ROL_CONDUCTOR},
    )
    conn.execute(text("DELETE FROM usuarios WHERE rol = :r"), {"r": ROL_CONDUCTOR})


def validar(conn: Connection, antes: dict[str, int], huellas_antes: dict[str, str]) -> list[str]:
    """Reglas que deben cumplirse antes de confirmar. Devuelve la lista de errores (vacía = todo bien)."""
    errores: list[str] = []
    despues = conteos(conn)
    for t in TABLAS_A_VACIAR:
        if despues.get(t, 0) != 0:
            errores.append(f"La tabla {t} debía quedar vacía y tiene {despues[t]} registro(s).")
    if despues.get("usuarios_conductor", 0) != 0:
        errores.append(f"Quedan {despues['usuarios_conductor']} usuario(s) con rol conductor.")
    for t in (*TABLAS_CONSERVADAS, "usuarios_oficina"):
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

    borrados = {t: antes[t] for t in (*TABLAS_A_VACIAR, "usuarios_conductor") if antes.get(t)}
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
                "conservados": {t: antes.get(t, 0) for t in (*TABLAS_CONSERVADAS, "usuarios_oficina")},
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
            log("Vaciando remesas, facturas, conductores, camiones e historial…")
            if conn.dialect.name == "postgresql":
                _vaciar_postgres(conn, existentes)
            else:
                _vaciar_sqlite(conn, existentes)
            _borrar_usuarios_conductores(conn)
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
    "usuarios_conductor": "Usuarios con rol conductor",
    "camiones": "Camiones",
    "auditoria": "Auditoría de seguridad",
    "bitacora": "Bitácora",
    "bitacora_archivos": "Bitácora archivada",
    "consultas_publicas": "Consultas públicas",
    "empresas": "Empresas",
    "vendedores": "Vendedores",
    "feriados": "Feriados",
    "usuarios_oficina": "Usuarios de oficina",
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
    "camiones",
    "auditoria",
    "bitacora",
    "bitacora_archivos",
    "consultas_publicas",
)
_CONSERVA = ("empresas", "vendedores", "feriados", "usuarios_oficina")


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
        description="Vacía remesas, facturas, conductores e historial; conserva "
        "empresas, vendedores, feriados y usuarios de oficina.",
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
