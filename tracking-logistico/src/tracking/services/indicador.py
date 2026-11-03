"""Reporte «Indicador de Gestión»: facturas con el tiempo de jornada laboral (días de 9 h) entre procesos.

Filtros: rango de fechas sobre la fecha elegida (Fecha Factura por defecto, Fecha Preparado, Fecha
Entregado o Fecha Estatus), estatus (uno o varios), cliente (contiene), vendedor, zona (= Destino de
la remesa) y empresa de transporte. Multi-tenant: un Transportista solo ve su empresa.

Primero la factura y el tiempo de cada tramo y el total para la entrega; luego las columnas del reporte
de Cobranzas (sin montos para quien no es SUPERVISOR ni COBRANZAS). SUMA y PROMEDIO son solo de tiempos.
En una devolución el último tramo termina en el registro de la devolución. Cada duración viaja como texto
(«2 días 3 h 05 min») y en minutos hábiles (``<clave>_min``) para ordenar, sumar y promediar.
"""

from __future__ import annotations

import io
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Final

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, selectinload

from tracking.enums import Estatus
from tracking.models import EmpresaTransporte, Factura, Remesa, Vendedor
from tracking.services.feriados import fechas as fechas_feriados
from tracking.services.reportes import validar_rango
from tracking.services.tiempos import dias_entre_procesos, formato_duracion, texto_jornada
from tracking.services.timeutil import local_day_bounds, now_utc, to_local
from tracking.tenancy import scope_de_sesion

CAMPOS_FECHA: Final[dict[str, str]] = {
    "fecha_fac": "Fecha Factura",
    "fecha_alm": "Fecha Preparado",
    "fecha_rec": "Fecha Entregado",
    "estatus_at": "Fecha Estatus",
}
MAX_FILAS: Final = 20000
MONTOS: Final = frozenset({"total_fact", "total_neto", "total_flete"})
DURACIONES: Final = ("t_preparado", "t_por_entregar", "t_entrega", "t_total")

# (clave, título, tipo). Pantalla, impresión y Excel muestran exactamente estas columnas y en este orden.
COLUMNAS: Final[tuple[tuple[str, str, str], ...]] = (
    ("numero", "Factura", "texto"),
    ("cliente", "Cliente", "texto"),
    ("estatus", "Estatus", "texto"),
    ("fecha_fac", "Fecha de factura", "fecha"),
    ("estatus_at", "Fecha último estatus", "fechahora"),
    ("t_preparado", "Tiempo de Facturado a Preparado", "duracion"),
    ("t_por_entregar", "Tiempo de Preparado a Por Entregar", "duracion"),
    ("t_entrega", "Tiempo de Por Entregar a Entregado o Devolución", "duracion"),
    ("observacion_tiempo", "Observación", "texto"),
    ("cod_vendedor", "Cod. Vendedor", "texto"),
    ("destino", "Zona", "texto"),
    ("chofer_nombre", "Chofer", "texto"),
    ("empresa_transporte", "Empresa de transporte", "texto"),
)
# Series de la gráfica: «entregada» agrupa Entregada + Devolución
SERIES: Final = ("recibida", "en_transito", "entregada")


class FiltroInvalido(ValueError):
    """Filtro incoherente (se responde 422)."""


@dataclass
class FiltrosIndicador:
    desde: date | None = None
    hasta: date | None = None
    campo_fecha: str = "fecha_fac"
    estatus: list[str] = field(default_factory=list)
    cliente: str | None = None
    cod_vendedor: str | None = None
    zona: str | None = None
    empresa_id: int | None = None
    # Clic en la gráfica: la tabla muestra solo esa serie (y esa empresa de transporte, si se eligió una barra)
    serie: str | None = None
    transporte: str | None = None

    def descripcion(self) -> str:
        d, h = validar_rango(self.desde, self.hasta)
        partes = [f"{CAMPOS_FECHA[self.campo_fecha]} del {d:%d/%m/%Y} al {h:%d/%m/%Y}"]
        if self.estatus:
            partes.append("Estatus: " + ", ".join(Estatus(e).label for e in self.estatus))
        if self.cliente:
            partes.append(f"Cliente contiene «{self.cliente}»")
        if self.cod_vendedor:
            partes.append(f"Vendedor {self.cod_vendedor}")
        if self.zona:
            partes.append(f"Zona {self.zona}")
        return " · ".join(partes)

    def descripcion_seleccion(self) -> str | None:
        if not self.serie:
            return None
        nombre = {"recibida": "Preparado", "en_transito": "Por Entregar",
                  "entregada": "Entregado (incluye devoluciones)"}
        return f"{nombre.get(self.serie, self.serie)} · {self.transporte or 'todas las empresas de transporte'}"


def columnas(con_montos: bool = False) -> tuple[tuple[str, str, str], ...]:
    """Las 13 columnas del indicador (sin montos para ningún rol)."""
    return COLUMNAS


def seleccion(filas: list[dict[str, Any]], f: FiltrosIndicador) -> list[dict[str, Any]]:
    """Facturas de la serie / empresa de transporte elegidas con clic en la gráfica (o todas)."""
    if f.serie and f.serie not in SERIES:
        raise FiltroInvalido(f"Serie desconocida: {f.serie}")
    if not f.serie and not f.transporte:
        return filas
    salida = []
    for x in filas:
        if f.serie and _serie(x["estatus_clave"]) != f.serie:
            continue
        if f.transporte and (x.get("empresa_transporte") or SIN_TRANSPORTE) != f.transporte:
            continue
        salida.append(x)
    return salida


def _consulta(db: Session, f: FiltrosIndicador) -> Select[tuple[Factura]]:
    if f.campo_fecha not in CAMPOS_FECHA:
        raise FiltroInvalido(f"Campo de fecha desconocido: {f.campo_fecha}")
    for e in f.estatus:
        if e not in {x.value for x in Estatus}:
            raise FiltroInvalido(f"Estatus desconocido: {e}")
    desde, hasta = validar_rango(f.desde, f.hasta)
    stmt = (
        select(Factura)
        .join(Remesa, Remesa.id == Factura.remesa_id)
        .options(selectinload(Factura.remesa).selectinload(Remesa.empresa_transporte))
    )
    stmt = scope_de_sesion(db).restringir(stmt, Factura)
    if f.campo_fecha == "fecha_fac":
        stmt = stmt.where(Factura.fecha_fac >= desde, Factura.fecha_fac <= hasta)
    else:
        ini, fin = local_day_bounds(desde, hasta)
        col = getattr(Factura, f.campo_fecha)
        stmt = stmt.where(col >= ini, col < fin)
    if f.estatus:
        stmt = stmt.where(Factura.estatus.in_(f.estatus))
    if f.cliente and f.cliente.strip():
        stmt = stmt.where(Factura.cliente.ilike(f"%{f.cliente.strip()}%"))
    if f.cod_vendedor:
        stmt = stmt.where(Factura.cod_vendedor == f.cod_vendedor.strip().upper())
    if f.zona:
        stmt = stmt.where(func.upper(func.trim(Remesa.destino)) == f.zona.strip().upper())
    if f.empresa_id:
        stmt = stmt.where(Factura.empresa_id == f.empresa_id)
    return stmt.order_by(Factura.fecha_fac, Factura.numero, Factura.id).limit(MAX_FILAS + 1)


def _local(x: datetime | None) -> datetime | None:
    return to_local(x) if x else None


def _fila(fa: Factura, vendedores: dict[str, str], ahora: datetime, feriados: Sequence[date]) -> dict[str, Any]:
    r = fa.remesa
    t = dias_entre_procesos(fa, ahora, feriados)
    tramos: dict[str, int | None] = {
        k: x.minutos for k, x in zip(("t_preparado", "t_por_entregar", "t_entrega"), t.tramos, strict=True)}
    tramos["t_total"] = t.total_minutos
    if t.devolucion:
        obs = t.mensaje
    elif t.en_curso:
        obs = "En curso (cuenta hasta ahora)"
    else:
        obs = t.aviso
    fila: dict[str, Any] = {
        "factura_id": fa.id,
        "empresa_transporte": r.empresa_transporte.nombre if r and r.empresa_transporte else None,
        "remesa_numero": r.numero if r else None,
        "destino": r.destino if r else None,
        "chofer_nombre": r.chofer_nombre if r else None,
        "cliente": fa.cliente, "numero": fa.numero, "pedido": fa.pedido, "cod_vendedor": fa.cod_vendedor,
        "vendedor": vendedores.get(fa.cod_vendedor or ""),
        "total_fact": fa.total_fact, "total_neto": fa.total_neto, "total_flete": fa.total_flete,
        "fecha_fac": fa.fecha_fac, "fecha_alm": _local(fa.fecha_alm), "fecha_tra": _local(fa.fecha_tra),
        # Devolución: en «Fecha Entregado / Devolución» va el momento de la devolución
        "fecha_rec": _local(fa.estatus_at if t.devolucion else fa.fecha_rec),
        "estatus": Estatus(fa.estatus).label, "estatus_clave": fa.estatus,
        "estatus_at": _local(fa.estatus_at), "observacion_tiempo": obs,
        # Para la gráfica (no es columna): empresa emisora de la factura (p. ej. IMPORTADORA USY)
        "empresa_emisora": fa.empresa,
    }
    for k, m in tramos.items():
        fila[k] = formato_duracion(m)
        fila[f"{k}_min"] = m
    return fila


def filas_indicador(db: Session, f: FiltrosIndicador, con_montos: bool) -> list[dict[str, Any]]:
    facturas = list(db.scalars(_consulta(db, f)))
    if len(facturas) > MAX_FILAS:
        raise FiltroInvalido(f"El reporte supera {MAX_FILAS} facturas: acorte el rango o agregue filtros.")
    vendedores = {v.codigo: v.nombre for v in db.scalars(select(Vendedor))}
    ahora = now_utc()
    dias_feriados = fechas_feriados(db)
    filas = [_fila(x, vendedores, ahora, dias_feriados) for x in facturas]
    if not con_montos:
        for fila in filas:
            for k in MONTOS:
                fila.pop(k, None)
    return filas


def _estadistica(filas: Sequence[dict[str, Any]], clave: str) -> dict[str, Any]:
    valores = [v for x in filas if (v := x.get(f"{clave}_min")) is not None]
    suma = sum(valores)
    prom = round(suma / len(valores)) if valores else None
    return {"suma_min": suma if valores else None, "suma": formato_duracion(suma) if valores else None,
            "promedio_min": prom, "promedio": formato_duracion(prom), "facturas": len(valores)}


def resumen(filas: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Conteos por estatus y SUMA / PROMEDIO de los tiempos entre procesos (nunca de montos)."""
    est = [x["estatus_clave"] for x in filas]
    entregadas = [x for x in filas if x["estatus_clave"] == Estatus.ENTREGADA.value]
    out: dict[str, Any] = {
        "facturas": len(filas),
        "entregadas": len(entregadas),
        "devoluciones": est.count(Estatus.DEVOLUCION.value),
        "por_entregar": est.count(Estatus.EN_TRANSITO.value),
        "preparadas": est.count(Estatus.RECIBIDA.value),
        "duraciones": {k: _estadistica(filas, k) for k in DURACIONES},
        # Promedio de entrega completa (solo Entregadas: Factura → Entregado)
        "promedio_entrega": _estadistica(entregadas, "t_total")["promedio"],
    }
    return out


def tabla(filas: list[dict[str, Any]], con_montos: bool, orden: str | None, desc: bool, limit: int,
          offset: int, filtros: FiltrosIndicador | None = None) -> dict[str, Any]:
    """Gráfica y KPIs con TODAS las facturas del filtro; la tabla (y su SUMA / PROMEDIO) con la selección."""
    todas = filas
    filas = seleccion(filas, filtros) if filtros else filas
    cols = columnas(con_montos)
    claves = {c for c, _, _ in cols}
    if orden in claves:
        k = f"{orden}_min" if orden in DURACIONES else orden

        def _clave(x: dict[str, Any]) -> Any:
            v = x[k]
            return v.casefold() if isinstance(v, str) else v

        con = sorted((x for x in filas if x.get(k) is not None), key=_clave, reverse=desc)
        filas = con + [x for x in filas if x.get(k) is None]
    return {
        "total": len(filas), "offset": offset, "limit": limit, "orden": orden if orden in claves else None,
        "desc": desc,
        "columnas": [{"clave": c, "titulo": t, "tipo": tp} for c, t, tp in cols],
        "items": filas[offset: offset + limit],
        "resumen": resumen(filas),
        "kpis": resumen(todas),
        "grafica": grafica(todas),
        "jornada": texto_jornada(),
        "seleccion": ({"serie": filtros.serie, "transporte": filtros.transporte,
                       "texto": filtros.descripcion_seleccion()} if filtros and filtros.serie else None),
    }


SIN_TRANSPORTE: Final = "Sin empresa de transporte"
SIN_EMISORA: Final = "Sin empresa emisora"


def _pct(n: int, total: int) -> float:
    return round(100.0 * n / total, 1) if total else 0.0


def _serie(estatus: str) -> str:
    """En la gráfica las devoluciones se suman a Entregado (son facturas cerradas)."""
    return Estatus.ENTREGADA.value if estatus == Estatus.DEVOLUCION.value else estatus


SERIES_GRAFICA: Final = (Estatus.RECIBIDA, Estatus.EN_TRANSITO, Estatus.ENTREGADA)


def grafica(filas: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Datos de la gráfica «Indicador de Gestión» (todas las filas del filtro, no solo la página).

    * Barras: una categoría por empresa de transporte (eje X) y tres series: Preparado, Por Entregar y
      Entregado. **Las devoluciones se suman a Entregado** (``devoluciones`` dice cuántas son). El porcentaje
      de cada barra es la parte de las facturas de ESA empresa de transporte que está en esa serie.
    * Desglose (clic en una barra o en la leyenda): por serie, las empresas emisoras con su cantidad y
      porcentaje; ``"*"`` = todas las empresas de transporte, o el nombre de una en particular.
    """
    series = [e.value for e in SERIES_GRAFICA]
    por_transporte: dict[str, Counter[str]] = {}
    devoluciones: Counter[str] = Counter()
    desglose: dict[str, dict[str, Counter[str]]] = {e: {"*": Counter()} for e in series}
    for x in filas:
        transp = x.get("empresa_transporte") or SIN_TRANSPORTE
        serie = _serie(x["estatus_clave"])
        emisora = x.get("empresa_emisora") or SIN_EMISORA
        por_transporte.setdefault(transp, Counter())[serie] += 1
        if x["estatus_clave"] == Estatus.DEVOLUCION.value:
            devoluciones[transp] += 1
        desglose[serie]["*"][emisora] += 1
        desglose[serie].setdefault(transp, Counter())[emisora] += 1
    orden = sorted(por_transporte, key=lambda n: (-sum(por_transporte[n].values()), n.casefold()))
    empresas = []
    for nombre in orden:
        c = por_transporte[nombre]
        total = sum(c.values())
        empresas.append({"nombre": nombre, "total": total, "cantidades": {e: c[e] for e in series},
                         "porcentajes": {e: _pct(c[e], total) for e in series},
                         "devoluciones": devoluciones[nombre]})

    def _sectores(c: Counter[str]) -> list[dict[str, Any]]:
        total = sum(c.values())
        return [{"empresa": em, "facturas": n, "porcentaje": _pct(n, total)}
                for em, n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0].casefold()))]

    titulos = {Estatus.ENTREGADA.value: "Entregado"}
    return {
        "titulo": "Indicador de Gestión",
        "estatus": [{"clave": e.value, "titulo": titulos.get(e.value, e.label)} for e in SERIES_GRAFICA],
        "empresas": empresas,
        "desglose": {e: {t: _sectores(c) for t, c in porT.items() if c} for e, porT in desglose.items()},
        "nota": "Entregado incluye las devoluciones.",
    }


def opciones(db: Session) -> dict[str, Any]:
    scope = scope_de_sesion(db)
    zonas = db.scalars(scope.restringir(
        select(func.upper(func.trim(Remesa.destino))).where(Remesa.destino.is_not(None)).distinct(), Remesa))
    vend = db.scalars(select(Vendedor).order_by(Vendedor.codigo))
    empresas = db.scalars(select(EmpresaTransporte).where(scope.criterio(EmpresaTransporte.id))
                          .order_by(EmpresaTransporte.nombre))
    return {
        "campos_fecha": [{"clave": k, "titulo": t} for k, t in CAMPOS_FECHA.items()],
        "estatus": [{"clave": e.value, "titulo": e.label} for e in Estatus],
        "zonas": sorted(z for z in zonas if z),
        "vendedores": [{"codigo": v.codigo, "nombre": v.nombre} for v in vend],
        "empresas": [{"id": e.id, "nombre": e.nombre} for e in empresas],
    }


# ------------------------------------------------------------------ Excel
# Exactamente estas columnas y en este orden (pedido de Cobranzas): sin montos, sin totales, sin títulos.
EXCEL_COLUMNAS: Final = COLUMNAS
_THIN = Side(style="thin")
_BORDE = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_GRIS = PatternFill("solid", fgColor="D9D9D9")
_FILL = {Estatus.ENTREGADA.value: PatternFill("solid", fgColor="C6EFCE"),
         Estatus.DEVOLUCION.value: PatternFill("solid", fgColor="FFC7CE")}
_ANCHOS: Final[dict[str, float]] = {
    "numero": 16, "cliente": 34, "estatus": 13, "fecha_fac": 13, "estatus_at": 17, "t_preparado": 20,
    "t_por_entregar": 20, "t_entrega": 22, "observacion_tiempo": 30, "cod_vendedor": 13, "destino": 14,
    "chofer_nombre": 22, "empresa_transporte": 26,
}


def generar_excel(filas: list[dict[str, Any]]) -> bytes:
    """Hoja tabular limpia: fila 1 = encabezados, luego una fila por factura (13 columnas fijas)."""
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Indicador de Gestión"
    for i, (_, titulo, _) in enumerate(EXCEL_COLUMNAS, start=1):
        c = ws.cell(1, i, titulo)
        c.font, c.fill, c.border = Font(name="Arial", size=10, bold=True), _GRIS, _BORDE
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = 32
    for r, fila in enumerate(filas, start=2):
        for i, (k, _, tipo) in enumerate(EXCEL_COLUMNAS, start=1):
            v = fila.get(k)
            if isinstance(v, datetime):
                v = v.replace(tzinfo=None)  # Excel no admite zona horaria (ya viene en hora local)
            c = ws.cell(r, i, v)
            c.font, c.border = Font(name="Arial", size=10), _BORDE
            if tipo == "fecha":
                c.number_format = "dd/mm/yyyy"
            elif tipo == "fechahora":
                c.number_format = "dd/mm/yyyy hh:mm"
            elif tipo == "duracion":
                c.alignment = Alignment(horizontal="right")
            if k == "estatus" and fila["estatus_clave"] in _FILL:
                c.fill = _FILL[fila["estatus_clave"]]
    for i, (k, _, _) in enumerate(EXCEL_COLUMNAS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = _ANCHOS[k]
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(EXCEL_COLUMNAS))}{max(len(filas) + 1, 1)}"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
