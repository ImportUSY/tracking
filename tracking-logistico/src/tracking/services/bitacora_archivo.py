"""Archivo de la bitácora por mes o por año.

La bitácora activa crece con cada petición. Archivar un período **cerrado** (un mes o un año que ya
terminó, en hora local) lo saca de la tabla activa y lo guarda comprimido en ``bitacora_archivos``:

1. Se leen las filas del período en orden y se escriben como JSON Lines comprimido con gzip (≈10 veces
   menos espacio que las filas con sus índices), con su SHA-256 para verificar que no se alteraron.
2. En la **misma transacción** se guarda el archivo y se borran esas filas de la bitácora activa. El
   borrado solo es posible aquí: el trigger de inmutabilidad lo permite únicamente dentro de un archivado
   (PostgreSQL: ``SET LOCAL tracking.archivando``; SQLite: fila temporal en ``bitacora_archivando``).
   Si algo falla, no se borra nada.
3. El archivo también es inmutable y se descarga como CSV (para Excel) o en su formato original.

Reglas: solo períodos cerrados; un período no se archiva dos veces; un año no se archiva si ya hay
meses suyos archivados (y al revés), para que cada fila esté en un único archivo.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, cast

from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from tracking.models import Bitacora, BitacoraArchivo, Usuario
from tracking.services.timeutil import local_day_bounds, now_utc, to_local, today_local

COLUMNAS = ("id", "ocurrido_at", "usuario_id", "usuario", "usuario_nombre", "rol", "empresa_id", "empresa_nombre",
            "accion", "modulo", "metodo", "ruta", "recurso_id", "estado_http", "exito", "detalle", "ip",
            "user_agent", "request_id", "duracion_ms")
MESES = ("Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre",
         "Noviembre", "Diciembre")


class ArchivoInvalido(ValueError):
    """Período mal escrito, abierto, ya archivado o vacío (se responde 409/422 con el mensaje)."""


@dataclass(frozen=True)
class Periodo:
    tipo: str  # "mes" | "anio"
    clave: str  # "2026-09" | "2026"
    inicio: date  # primer día (local)
    fin: date  # último día (local), incluido

    @property
    def nombre(self) -> str:
        return f"{MESES[self.inicio.month - 1]} {self.inicio.year}" if self.tipo == "mes" else f"Año {self.clave}"

    def limites_utc(self) -> tuple[datetime, datetime]:
        ini, fin = local_day_bounds(self.inicio, self.fin)
        assert ini is not None and fin is not None
        return ini, fin


def periodo(tipo: str, clave: str) -> Periodo:
    """«mes» + «2026-09» o «anio» + «2026»."""
    clave = (clave or "").strip()
    if tipo == "mes":
        m = re.fullmatch(r"(\d{4})-(\d{2})", clave)
        if not m or not 1 <= int(m[2]) <= 12:
            raise ArchivoInvalido("Indique el mes como AAAA-MM (por ejemplo 2026-09)")
        anio, mes = int(m[1]), int(m[2])
        siguiente = date(anio + (mes == 12), mes % 12 + 1, 1)
        return Periodo("mes", clave, date(anio, mes, 1), date.fromordinal(siguiente.toordinal() - 1))
    if tipo == "anio":
        if not re.fullmatch(r"\d{4}", clave):
            raise ArchivoInvalido("Indique el año con 4 cifras (por ejemplo 2025)")
        return Periodo("anio", clave, date(int(clave), 1, 1), date(int(clave), 12, 31))
    raise ArchivoInvalido("El tipo debe ser «mes» o «anio»")


def _validar(db: Session, p: Periodo, hoy: date) -> None:
    if p.fin >= hoy:
        raise ArchivoInvalido(f"{p.nombre} todavía no ha terminado: solo se archivan períodos cerrados")
    if db.scalar(select(BitacoraArchivo.id).where(BitacoraArchivo.periodo == p.clave)) is not None:
        raise ArchivoInvalido(f"{p.nombre} ya está archivado")
    anio = p.clave[:4]
    if p.tipo == "anio":
        meses = list(db.scalars(select(BitacoraArchivo.periodo).where(BitacoraArchivo.periodo.like(f"{anio}-%"))))
        if meses:
            raise ArchivoInvalido(f"Ya hay meses de {anio} archivados ({', '.join(sorted(meses))}): archive los meses "
                                  "que faltan en lugar del año completo")
    elif db.scalar(select(BitacoraArchivo.id).where(BitacoraArchivo.periodo == anio)) is not None:
        raise ArchivoInvalido(f"El año {anio} completo ya está archivado")


def _fila(b: Bitacora) -> dict[str, Any]:
    d = {c: getattr(b, c) for c in COLUMNAS}
    d["ocurrido_at"] = b.ocurrido_at.isoformat()
    return d


def _permitir_borrado(db: Session) -> None:
    """Abre el «candado» del trigger solo para esta transacción."""
    if db.get_bind().dialect.name == "sqlite":
        db.execute(text("INSERT INTO bitacora_archivando (id) VALUES (1)"))
    else:
        db.execute(text("SET LOCAL tracking.archivando = 'si'"))


def _cerrar_candado(db: Session) -> None:
    if db.get_bind().dialect.name == "sqlite":
        db.execute(text("DELETE FROM bitacora_archivando"))


def archivar(db: Session, tipo: str, clave: str, actor: Usuario | None, hoy: date | None = None) -> BitacoraArchivo:
    """Archiva el período y borra sus filas de la bitácora activa. Hace commit (todo o nada)."""
    p = periodo(tipo, clave)
    _validar(db, p, hoy or today_local())
    ini, fin = p.limites_utc()
    filtro = (Bitacora.ocurrido_at >= ini, Bitacora.ocurrido_at < fin)

    crudo = io.BytesIO()
    sha = hashlib.sha256()
    filas, primer, ultimo = 0, None, None
    with gzip.GzipFile(fileobj=crudo, mode="wb", mtime=0) as gz:
        for b in db.scalars(select(Bitacora).where(*filtro).order_by(Bitacora.id).execution_options(yield_per=2000)):
            gz.write((json.dumps(_fila(b), ensure_ascii=False, default=str) + "\n").encode("utf-8"))
            filas += 1
            primer = primer if primer is not None else b.id
            ultimo = b.id
    if not filas:
        raise ArchivoInvalido(f"No hay registros de bitácora en {p.nombre}")
    contenido = crudo.getvalue()
    sha.update(contenido)
    archivo = BitacoraArchivo(
        tipo=p.tipo, periodo=p.clave, desde=ini, hasta=fin, filas=filas, primer_id=primer, ultimo_id=ultimo,
        bytes=len(contenido), sha256=sha.hexdigest(), formato="jsonl.gz", contenido=contenido,
        creado_por_id=actor.id if actor else None, creado_por=actor.nombre if actor else "sistema",
        created_at=now_utc(),
    )
    try:
        db.add(archivo)
        db.flush()
        _permitir_borrado(db)
        resultado = db.execute(
            delete(Bitacora).where(*filtro).execution_options(synchronize_session=False)
        )
        borradas = cast(CursorResult[Any], resultado).rowcount
        _cerrar_candado(db)
        if borradas != filas:  # llegó o desapareció algo entre la lectura y el borrado: no se archiva
            raise ArchivoInvalido("La bitácora cambió durante el archivado; intente de nuevo")
        db.commit()
    except Exception:
        db.rollback()
        raise
    return archivo


def pendientes(db: Session, hoy: date | None = None) -> list[dict[str, Any]]:
    """Meses (y años) cerrados que aún tienen registros en la bitácora activa: sugerencias para archivar.

    Cuenta mes a mes con el índice de ``ocurrido_at`` (no recorre toda la tabla)."""
    hoy = hoy or today_local()
    primero = db.scalar(select(func.min(Bitacora.ocurrido_at)))
    if primero is None:
        return []
    inicio = to_local(primero).date().replace(day=1)
    conteo: dict[str, int] = {}
    anio, mes = inicio.year, inicio.month
    while (anio, mes) < (hoy.year, hoy.month):
        p = periodo("mes", f"{anio:04d}-{mes:02d}")
        ini, fin = p.limites_utc()
        n = int(db.scalar(select(func.count(Bitacora.id)).where(Bitacora.ocurrido_at >= ini,
                                                                Bitacora.ocurrido_at < fin)) or 0)
        if n:
            conteo[p.clave] = n
        anio, mes = (anio + 1, 1) if mes == 12 else (anio, mes + 1)
    salida = [{"tipo": "mes", "periodo": k, "nombre": periodo("mes", k).nombre, "filas": v}
              for k, v in sorted(conteo.items())]
    for a in sorted({k[:4] for k in conteo}):
        if int(a) < hoy.year:
            salida.append({"tipo": "anio", "periodo": a, "nombre": f"Año {a}",
                           "filas": sum(v for k, v in conteo.items() if k.startswith(a))})
    return salida


def total_activo(db: Session) -> int:
    return int(db.scalar(select(func.count(Bitacora.id))) or 0)


def leer(archivo: BitacoraArchivo) -> Iterator[dict[str, Any]]:
    """Filas originales del archivo, verificando antes su integridad (SHA-256)."""
    if hashlib.sha256(archivo.contenido).hexdigest() != archivo.sha256:
        raise ArchivoInvalido(f"El archivo {archivo.periodo} no pasa la verificación de integridad")
    with gzip.open(io.BytesIO(archivo.contenido), "rt", encoding="utf-8") as f:
        for linea in f:
            yield json.loads(linea)


def a_csv(archivo: BitacoraArchivo) -> Iterator[bytes]:
    """CSV con «;» y BOM UTF-8: se abre directo en Excel en español, con fecha y hora local."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["id", "Fecha y hora", *COLUMNAS[2:]])
    yield ("﻿" + buf.getvalue()).encode("utf-8")
    for fila in leer(archivo):
        buf.seek(0)
        buf.truncate()
        momento = to_local(datetime.fromisoformat(fila["ocurrido_at"]))
        valores = [json.dumps(fila[c], ensure_ascii=False) if c == "detalle" else fila[c] for c in COLUMNAS[2:]]
        w.writerow([fila["id"], momento.strftime("%d/%m/%Y %H:%M:%S"), *["" if v is None else v for v in valores]])
        yield buf.getvalue().encode("utf-8")
