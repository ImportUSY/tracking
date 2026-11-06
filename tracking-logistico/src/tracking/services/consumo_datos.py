"""Datos de navegación: consumo de datos móviles (red celular) de la app del conductor.

El teléfono mide lo que la app envía y recibe solo mientras está conectado por **red celular**
(``navigator.connection.type == "cellular"``); lo que pasa por Wi-Fi nunca se registra. Agrupa por
conexión (cada vez que se abre la app), método + ruta, día y hora local, y cada pocos minutos envía las
sumas parciales: el servidor las **acumula** en la fila de la misma clave (reenviar no duplica la fila).
Cada fila guarda la fecha y hora exactas de su primera (``desde``) y su última (``hasta``) petición.

Es una medición de la aplicación (cuerpos y cabeceras HTTP que ve el navegador), no el contador del
sistema operativo: sirve para comparar conductores, rutas y días, no para conciliar la factura del plan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Final

from sqlalchemy import Select, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tracking.models import ConsumoDatos, EmpresaTransporte, Usuario
from tracking.services.reportes import validar_rango
from tracking.services.timeutil import now_utc, to_local

AGRUPACIONES: Final = ("detalle", "conductor", "dia", "ruta", "conexion")
_ID_EN_RUTA: Final = re.compile(r"/(?:\d+|[0-9a-fA-F-]{16,36})(?=/|$)")


class FiltroConsumoInvalido(ValueError):
    """Filtro incoherente (se responde 422)."""


def normalizar_ruta(ruta: str) -> str:
    """``/api/fotos/3f2a…?x=1`` → ``/api/fotos/:id``: agrupa las peticiones iguales a distintos recursos."""
    ruta = ruta.split("?", 1)[0].split("#", 1)[0] or "/"
    return _ID_EN_RUTA.sub("/:id", ruta)[:200]


@dataclass(frozen=True)
class RegistroConsumo:
    dia: date
    conexion_id: str
    metodo: str
    ruta: str
    tipo_efectivo: str | None
    solicitudes: int
    bytes_subida: int
    bytes_bajada: int
    desde: datetime
    hasta: datetime
    hora: int | None = None


def _utc(x: datetime) -> datetime:
    x = x if x.tzinfo else x.replace(tzinfo=timezone.utc)
    return min(x, now_utc())


def registrar(db: Session, usuario: Usuario, registros: list[RegistroConsumo]) -> int:
    """Suma cada registro a la fila de su clave (usuario, conexión, método, ruta, día y hora). Hace commit."""
    empresa = db.get(EmpresaTransporte, usuario.empresa_id) if usuario.empresa_id else None
    n = 0
    for r in registros:
        ruta, metodo = normalizar_ruta(r.ruta), r.metodo.upper()[:8]
        hora = r.hora if r.hora is not None else to_local(_utc(r.desde)).hour
        clave = (ConsumoDatos.usuario_id == usuario.id, ConsumoDatos.conexion_id == r.conexion_id,
                 ConsumoDatos.metodo == metodo, ConsumoDatos.ruta == ruta, ConsumoDatos.dia == r.dia,
                 ConsumoDatos.hora == hora)
        for intento in range(2):
            fila = db.scalar(select(ConsumoDatos).where(*clave))
            desde, hasta = _utc(r.desde), _utc(r.hasta)
            if fila is None:
                fila = ConsumoDatos(
                    dia=r.dia, hora=hora, usuario_id=usuario.id, usuario=usuario.username,
                    usuario_nombre=usuario.nombre,
                    empresa_id=usuario.empresa_id, empresa_nombre=empresa.nombre if empresa else None,
                    conexion_id=r.conexion_id, tipo_red="cellular", metodo=metodo, ruta=ruta,
                    tipo_efectivo=r.tipo_efectivo, solicitudes=0, bytes_subida=0, bytes_bajada=0,
                    desde=desde, hasta=hasta,
                )
                db.add(fila)
            fila.solicitudes += r.solicitudes
            fila.bytes_subida += r.bytes_subida
            fila.bytes_bajada += r.bytes_bajada
            fila.desde, fila.hasta = min(fila.desde, desde), max(fila.hasta, hasta)
            fila.tipo_efectivo = r.tipo_efectivo or fila.tipo_efectivo
            try:
                db.commit()
                break
            except IntegrityError:  # otra petición creó la misma fila al mismo tiempo: se suma a esa
                db.rollback()
                if intento:
                    raise
        n += 1
    return n


@dataclass
class FiltrosConsumo:
    desde: date | None = None
    hasta: date | None = None
    usuario_id: int | None = None
    empresa_id: int | None = None
    ruta: str | None = None
    conexion_id: str | None = None
    q: str | None = None


def _filtrar(stmt: Select[Any], f: FiltrosConsumo) -> Select[Any]:
    if f.desde or f.hasta:
        d, h = validar_rango(f.desde, f.hasta)
        stmt = stmt.where(ConsumoDatos.dia >= d, ConsumoDatos.dia <= h)
    if f.usuario_id is not None:
        stmt = stmt.where(ConsumoDatos.usuario_id == f.usuario_id)
    if f.empresa_id is not None:
        stmt = stmt.where(ConsumoDatos.empresa_id == f.empresa_id)
    if f.ruta and f.ruta.strip():
        stmt = stmt.where(ConsumoDatos.ruta.ilike(f"%{f.ruta.strip()}%"))
    if f.conexion_id and f.conexion_id.strip():
        stmt = stmt.where(ConsumoDatos.conexion_id.ilike(f"{f.conexion_id.strip()}%"))
    if f.q and f.q.strip():
        like = f"%{f.q.strip()}%"
        stmt = stmt.where(or_(ConsumoDatos.usuario.ilike(like), ConsumoDatos.usuario_nombre.ilike(like),
                              ConsumoDatos.ruta.ilike(like)))
    return stmt


_SUMAS: Final = (
    func.sum(ConsumoDatos.solicitudes).label("solicitudes"),
    func.sum(ConsumoDatos.bytes_subida).label("bytes_subida"),
    func.sum(ConsumoDatos.bytes_bajada).label("bytes_bajada"),
    func.min(ConsumoDatos.desde).label("desde"),
    func.max(ConsumoDatos.hasta).label("hasta"),
)
_GRUPOS: Final[dict[str, tuple[Any, ...]]] = {
    "detalle": (ConsumoDatos.dia, ConsumoDatos.hora, ConsumoDatos.usuario_id, ConsumoDatos.conexion_id,
                ConsumoDatos.metodo, ConsumoDatos.ruta),
    "conductor": (ConsumoDatos.usuario_id,),
    "dia": (ConsumoDatos.dia,),
    "ruta": (ConsumoDatos.metodo, ConsumoDatos.ruta),
    "conexion": (ConsumoDatos.usuario_id, ConsumoDatos.conexion_id),
}
_ORDEN: Final[dict[str, tuple[Any, ...]]] = {
    "detalle": (ConsumoDatos.dia.desc(), func.max(ConsumoDatos.hasta).desc()),
    "conductor": (),
    "dia": (ConsumoDatos.dia.desc(),),
    "ruta": (),
    "conexion": (func.min(ConsumoDatos.desde).desc(),),
}


def consultar(db: Session, f: FiltrosConsumo, agrupar: str, limit: int, offset: int) -> dict[str, Any]:
    """Filas agrupadas (de mayor a menor consumo, salvo detalle/día/conexión: las más recientes) y totales."""
    if agrupar not in AGRUPACIONES:
        raise FiltroConsumoInvalido(f"Agrupación desconocida: {agrupar}")
    grupo = _GRUPOS[agrupar]
    total_bytes = func.sum(ConsumoDatos.bytes_subida + ConsumoDatos.bytes_bajada)
    extra = (
        func.max(ConsumoDatos.usuario).label("usuario"),
        func.max(ConsumoDatos.usuario_nombre).label("usuario_nombre"),
        func.max(ConsumoDatos.empresa_nombre).label("empresa_nombre"),
        func.max(ConsumoDatos.tipo_efectivo).label("tipo_efectivo"),
        func.count(func.distinct(ConsumoDatos.conexion_id)).label("conexiones"),
        func.count(func.distinct(ConsumoDatos.usuario_id)).label("conductores"),
    )
    stmt = _filtrar(select(*grupo, *_SUMAS, *extra), f).group_by(*grupo)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    orden = _ORDEN[agrupar] or (total_bytes.desc(),)
    filas = db.execute(stmt.order_by(*orden).limit(limit).offset(offset)).mappings().all()
    items = []
    for x in filas:
        fila = {k: x[k] for k in x.keys()}  # noqa: SIM118 (RowMapping no se itera como dict)
        fila["bytes_total"] = int(fila["bytes_subida"] or 0) + int(fila["bytes_bajada"] or 0)
        items.append(fila)
    tot = db.execute(_filtrar(select(*_SUMAS[:3], func.count(func.distinct(ConsumoDatos.conexion_id)),
                                     func.count(func.distinct(ConsumoDatos.usuario_id))), f)).one()
    subida, bajada = int(tot[1] or 0), int(tot[2] or 0)
    return {
        "agrupar": agrupar, "total": int(total), "items": items,
        "totales": {"solicitudes": int(tot[0] or 0), "bytes_subida": subida, "bytes_bajada": bajada,
                    "bytes_total": subida + bajada, "conexiones": int(tot[3] or 0), "conductores": int(tot[4] or 0)},
    }
