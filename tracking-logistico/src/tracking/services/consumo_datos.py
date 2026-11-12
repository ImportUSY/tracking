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

import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Final

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
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
        "grafica": grafica(db, f),
    }


def _por(db: Session, f: FiltrosConsumo, grupo: tuple[Any, ...], limite: int | None) -> list[dict[str, Any]]:
    total_bytes = func.sum(ConsumoDatos.bytes_subida + ConsumoDatos.bytes_bajada)
    stmt = _filtrar(select(
        *grupo, *_SUMAS,
        func.max(ConsumoDatos.usuario).label("usuario"),
        func.max(ConsumoDatos.usuario_nombre).label("usuario_nombre"),
        func.max(ConsumoDatos.empresa_nombre).label("empresa_nombre"),
        func.count(func.distinct(ConsumoDatos.conexion_id)).label("conexiones"),
    ), f).group_by(*grupo).order_by(total_bytes.desc())
    if limite:
        stmt = stmt.limit(limite)
    salida = []
    for x in db.execute(stmt).mappings():
        sub, baj = int(x["bytes_subida"] or 0), int(x["bytes_bajada"] or 0)
        salida.append({
            "usuario_id": x["usuario_id"], "conexion_id": x.get("conexion_id"),
            "usuario": x["usuario"], "usuario_nombre": x["usuario_nombre"], "empresa_nombre": x["empresa_nombre"],
            "conexiones": int(x["conexiones"] or 0), "solicitudes": int(x["solicitudes"] or 0),
            "bytes_subida": sub, "bytes_bajada": baj, "bytes_total": sub + baj,
            "desde": x["desde"], "hasta": x["hasta"],
        })
    return salida


GRAFICA_CONDUCTORES: Final = 20
GRAFICA_CONEXIONES: Final = 15


def grafica(db: Session, f: FiltrosConsumo) -> dict[str, Any]:
    """Datos de las gráficas (todas las filas del filtro): Enviado y Recibido por conductor y las conexiones
    de mayor consumo total."""
    return {
        "conductores": _por(db, f, (ConsumoDatos.usuario_id,), GRAFICA_CONDUCTORES),
        "conexiones": _por(db, f, (ConsumoDatos.usuario_id, ConsumoDatos.conexion_id), GRAFICA_CONEXIONES),
    }


# ------------------------------------------------------------------ Excel
_COLUMNAS_EXCEL: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    "detalle": (("fecha_hora", "Fecha y hora"), ("usuario_nombre", "Conductor"), ("usuario", "Usuario"),
                ("empresa_nombre", "Empresa"), ("conexion_id", "Conexión"), ("metodo", "Método"),
                ("ruta", "Ruta"), ("tipo_efectivo", "Red")),
    "conductor": (("usuario_nombre", "Conductor"), ("usuario", "Usuario"), ("empresa_nombre", "Empresa"),
                  ("fecha_hora", "Fecha y hora"), ("conexiones", "Conexiones")),
    "dia": (("dia", "Fecha"), ("fecha_hora", "Fecha y hora"), ("conductores", "Conductores"),
            ("conexiones", "Conexiones")),
    "ruta": (("metodo", "Método"), ("ruta", "Ruta"), ("fecha_hora", "Fecha y hora"), ("conductores", "Conductores")),
    "conexion": (("conexion_id", "Conexión"), ("usuario_nombre", "Conductor"), ("usuario", "Usuario"),
                 ("fecha_hora", "Fecha y hora"), ("tipo_efectivo", "Red")),
}
_BYTES_EXCEL: Final = (("solicitudes", "Solicitudes"), ("kb_subida", "Enviado (KB)"),
                       ("kb_bajada", "Recibido (KB)"), ("kb_total", "Total (KB)"))
MAX_FILAS_EXCEL: Final = 20000


def _fecha_hora(x: dict[str, Any]) -> str:
    if not x.get("desde"):
        return ""
    d, h = to_local(_utc(x["desde"])), to_local(_utc(x["hasta"] or x["desde"]))
    if d == h:
        return f"{d:%d/%m/%Y %H:%M:%S}"
    return f"{d:%d/%m/%Y %H:%M:%S} – " + (f"{h:%H:%M:%S}" if d.date() == h.date() else f"{h:%d/%m/%Y %H:%M:%S}")


def _kb(n: int) -> float:
    return round(n / 1024, 1)


def generar_excel(db: Session, f: FiltrosConsumo, agrupar: str) -> bytes:
    """Hoja 1: las filas de la agrupación elegida. Hoja 2: por conductor y hoja 3: por conexión, cada una con
    su gráfica (Enviado / Recibido y consumo total)."""
    datos = consultar(db, f, agrupar, MAX_FILAS_EXCEL, 0)
    lado = Side(style="thin")
    borde = Border(left=lado, right=lado, top=lado, bottom=lado)
    gris = PatternFill("solid", fgColor="D9D9D9")

    def hoja(ws: Any, columnas: tuple[tuple[str, str], ...], filas: list[dict[str, Any]]) -> None:
        for i, (_, titulo) in enumerate(columnas, start=1):
            c = ws.cell(1, i, titulo)
            c.font, c.fill, c.border = Font(name="Arial", size=10, bold=True), gris, borde
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for r, x in enumerate(filas, start=2):
            x = dict(x, fecha_hora=_fecha_hora(x), kb_subida=_kb(x["bytes_subida"]),
                     kb_bajada=_kb(x["bytes_bajada"]), kb_total=_kb(x["bytes_total"]))
            for i, (k, _) in enumerate(columnas, start=1):
                c = ws.cell(r, i, x.get(k))
                c.font, c.border = Font(name="Arial", size=10), borde
                if k == "dia":
                    c.number_format = "dd/mm/yyyy"
                elif k.startswith("kb_"):
                    c.number_format = "#,##0.0"
        for i, (k, titulo) in enumerate(columnas, start=1):
            ancho = {"ruta": 38, "fecha_hora": 30, "usuario_nombre": 26, "empresa_nombre": 24, "conexion_id": 38}
            ws.column_dimensions[get_column_letter(i)].width = ancho.get(k, max(12, len(titulo) + 2))
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(columnas))}{max(len(filas) + 1, 1)}"

    def grafico(ws: Any, titulo: str, n: int, col_cat: int, cols: tuple[int, ...], celda: str) -> None:
        if not n:
            return
        g = BarChart()
        g.type, g.title, g.y_axis.title = "col", titulo, "KB"
        g.height, g.width = 9, max(18, n * 1.6)
        for col in cols:
            g.add_data(Reference(ws, min_col=col, min_row=1, max_row=n + 1), titles_from_data=True)
        g.set_categories(Reference(ws, min_col=col_cat, min_row=2, max_row=n + 1))
        ws.add_chart(g, celda)

    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Datos de Navegación"
    hoja(ws, _COLUMNAS_EXCEL[agrupar] + _BYTES_EXCEL, datos["items"])

    por_cond = _por(db, f, (ConsumoDatos.usuario_id,), None)
    cols_cond = (("usuario_nombre", "Conductor"), ("usuario", "Usuario"), ("empresa_nombre", "Empresa"),
                 ("conexiones", "Conexiones"), ("fecha_hora", "Fecha y hora"), *_BYTES_EXCEL)
    ws2 = wb.create_sheet("Por conductor")
    hoja(ws2, cols_cond, por_cond)
    base = len(cols_cond) - 2  # Enviado, Recibido y Total son las 3 últimas columnas
    grafico(ws2, "Enviado y Recibido por conductor (KB)", min(len(por_cond), 30), 1, (base, base + 1),
            f"A{len(por_cond) + 4}")

    por_con = _por(db, f, (ConsumoDatos.usuario_id, ConsumoDatos.conexion_id), None)
    cols_con = (("conexion_id", "Conexión"), ("usuario_nombre", "Conductor"), ("usuario", "Usuario"),
                ("fecha_hora", "Fecha y hora"), *_BYTES_EXCEL)
    ws3 = wb.create_sheet("Por conexión")
    hoja(ws3, cols_con, por_con)
    grafico(ws3, "Consumo total por conexión (KB)", min(len(por_con), 30), 2, (len(cols_con),),
            f"A{len(por_con) + 4}")

    for w in (ws, ws2, ws3):
        w.page_setup.orientation = "landscape"
        w.page_setup.fitToWidth, w.page_setup.fitToHeight = 1, 0
        w.sheet_properties.pageSetUpPr.fitToPage = True
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
