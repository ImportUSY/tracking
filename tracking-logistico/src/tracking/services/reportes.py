"""Consultas de reporte: selección para Cobranzas, avisos a vendedores y métricas del PRD.

Multi-tenant: cada consulta se acota con el ``TenantScope`` de la sesión (un Transportista solo
exporta, notifica y mide lo de su empresa). El filtro global de ``tracking.tenancy`` lo garantiza
igualmente; aquí se hace explícito para que el reporte sea correcto también fuera de una petición.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from tracking.enums import Estatus, OrigenEvento
from tracking.models import ConsultaPublica, EventoEstatus, Factura, Remesa
from tracking.services.cobranzas_excel import COLUMNAS_REMESA, MONTOS, columnas_factura, valores_factura, valores_remesa
from tracking.services.timeutil import local_day_bounds, to_local, today_local
from tracking.tenancy import scope_de_sesion

FINALES = (Estatus.ENTREGADA.value, Estatus.DEVOLUCION.value)
EN_CURSO = (Estatus.EN_TRANSITO.value,)  # Por Entregar
MAX_DIAS_RANGO = 366


class RangoInvalido(ValueError):
    """Desde/Hasta incoherentes (se responde 422 con el mensaje)."""


def validar_rango(desde: date | None, hasta: date | None) -> tuple[date, date]:
    """Rango de días locales del filtro. Sin fechas = hoy; con una sola, ese día. Desde ≤ Hasta."""
    if desde is None and hasta is None:
        desde = hasta = today_local()
    desde = desde or hasta
    hasta = hasta or desde
    assert desde is not None and hasta is not None
    if desde > hasta:
        raise RangoInvalido(f"La fecha Desde ({desde:%d/%m/%Y}) no puede ser posterior a Hasta ({hasta:%d/%m/%Y}).")
    if (hasta - desde).days >= MAX_DIAS_RANGO:
        raise RangoInvalido(f"El rango no puede superar {MAX_DIAS_RANGO} días: acórtelo o descárguelo por partes.")
    return desde, hasta


def seleccionar_para_cobranzas(
    db: Session,
    remesa_ids: Sequence[int] | None,
    desde: date | None,
    hasta: date | None,
    solo_finales: bool,
) -> list[tuple[Remesa, list[Factura]]]:
    """Remesas y facturas para Cobranzas.

    Por fechas: solo las facturas cuya **Fecha Estatus** (``estatus_at``, momento del último cambio de
    estatus) cae entre Desde y Hasta (días locales, ambos incluidos); cada remesa aparece con esas
    facturas. Con ``remesa_ids`` (selección explícita) van las remesas completas.
    """
    stmt = scope_de_sesion(db).restringir(
        select(Remesa).options(selectinload(Remesa.facturas)).order_by(Remesa.numero), Remesa
    )
    ini = fin = None
    if remesa_ids:
        stmt = stmt.where(Remesa.id.in_(list(remesa_ids)))
    else:
        ini, fin = local_day_bounds(*validar_rango(desde, hasta))
        assert ini is not None and fin is not None
        sub = select(Factura.remesa_id).where(Factura.estatus_at >= ini, Factura.estatus_at < fin)
        if solo_finales:
            sub = sub.where(Factura.estatus.in_(FINALES))
        stmt = stmt.where(Remesa.id.in_(sub))
    salida: list[tuple[Remesa, list[Factura]]] = []
    for remesa in db.scalars(stmt):
        facturas = [
            f for f in remesa.facturas
            if (not solo_finales or f.estatus in FINALES)
            and (ini is None or fin is None or ini <= f.estatus_at < fin)
        ]
        if facturas:
            salida.append((remesa, facturas))
    return salida


def filtrar_cobranzas(
    seleccion: Sequence[tuple[Remesa, list[Factura]]],
    *,
    cliente: str | None = None,
    conductor_id: int | None = None,
) -> list[tuple[Remesa, list[Factura]]]:
    """Filtros de la vista (y del Excel): cliente (contiene, sin mayúsculas) y conductor de la remesa."""
    texto = (cliente or "").strip().casefold()
    salida: list[tuple[Remesa, list[Factura]]] = []
    for remesa, facturas in seleccion:
        if conductor_id is not None and remesa.conductor_id != conductor_id:
            continue
        filtradas = [f for f in facturas if not texto or texto in (f.cliente or "").casefold()]
        if filtradas:
            salida.append((remesa, filtradas))
    return salida


def tabla_cobranzas(
    seleccion: Sequence[tuple[Remesa, list[Factura]]],
    *,
    orden: str | None = None,
    desc: bool = False,
    limit: int = 50,
    offset: int = 0,
    con_montos: bool = True,
) -> dict[str, Any]:
    """Mismo contenido que el Excel de Cobranzas, en filas planas para una tabla paginada y ordenable.

    Cada fila = datos del bloque de la remesa (chofer, placa, destino…) + columnas de la factura,
    calculados con las mismas funciones que el Excel. Los totales cubren todo el filtro, no solo la página.
    Sin ``con_montos`` (roles distintos de SUPERVISOR y COBRANZAS) no hay columnas ni totales de montos.
    """
    columnas = COLUMNAS_REMESA + columnas_factura(con_montos)
    filas: list[dict[str, Any]] = []
    for remesa, facturas in seleccion:
        base = {"remesa_id": remesa.id, **valores_remesa(remesa)}
        for f in facturas:
            valores = valores_factura(f)
            if not con_montos:
                valores = {k: v for k, v in valores.items() if k not in MONTOS}
            filas.append({**base, "factura_id": f.id, **valores})
    claves = {c for c, _, _ in columnas}
    if orden in claves:
        def _clave(fila: dict[str, Any]) -> tuple[bool, Any]:
            v = fila[orden]
            return (v is None, v.casefold() if isinstance(v, str) else (v if v is not None else 0))

        con_valor = sorted((f for f in filas if f[orden] is not None), key=_clave, reverse=desc)
        filas = con_valor + [f for f in filas if f[orden] is None]  # vacíos siempre al final

    def _suma(clave: str) -> Decimal:
        return sum((Decimal(str(f[clave])) for f in filas if f[clave] is not None), Decimal(0))

    return {
        "total": len(filas),
        "offset": offset,
        "limit": limit,
        "orden": orden if orden in claves else None,
        "desc": desc,
        "columnas": [{"clave": c, "titulo": t, "tipo": tp} for c, t, tp in columnas],
        "items": filas[offset: offset + limit],
        "totales": {
            "facturas": len(filas),
            "remesas": len({f["remesa_id"] for f in filas}),
            **({
                "total_fact": _suma("total_fact"),
                "total_neto": _suma("total_neto"),
                "total_flete": _suma("total_flete"),
            } if con_montos else {}),
        },
    }


def facturas_por_vendedor(
    db: Session,
    modo: str,
    desde: date | None,
    hasta: date | None,
    remesa_ids: Sequence[int] | None,
) -> dict[str, list[Factura]]:
    estatus = EN_CURSO if modo == "en_curso" else FINALES
    stmt = (
        select(Factura)
        .options(selectinload(Factura.remesa))
        .where(Factura.estatus.in_(estatus), Factura.cod_vendedor.is_not(None))
        .order_by(Factura.cod_vendedor, Factura.numero)
    )
    stmt = scope_de_sesion(db).restringir(stmt, Factura)
    if remesa_ids:
        stmt = stmt.where(Factura.remesa_id.in_(list(remesa_ids)))
    else:
        ini, fin = local_day_bounds(*validar_rango(desde, hasta))
        assert ini is not None and fin is not None
        stmt = stmt.where(Factura.estatus_at >= ini, Factura.estatus_at < fin)
    grupos: dict[str, list[Factura]] = defaultdict(list)
    for f in db.scalars(stmt):
        grupos[f.cod_vendedor or ""].append(f)
    return dict(grupos)


def metricas(db: Session, desde: date | None, hasta: date | None) -> dict[str, Any]:
    """Métrica estrella y de apoyo definidas en la sección 11 del PRD."""
    hasta = hasta or today_local()
    desde = desde or (hasta - timedelta(days=6))
    desde, hasta = validar_rango(desde, hasta)
    ini, fin = local_day_bounds(desde, hasta)
    assert ini is not None and fin is not None

    scope = scope_de_sesion(db)
    eventos = db.execute(
        select(EventoEstatus.ocurrido_at, EventoEstatus.recibido_at, EventoEstatus.origen)
        .join(Factura, Factura.id == EventoEstatus.factura_id)  # el evento hereda la empresa de su factura
        .where(scope.criterio(Factura.empresa_id))
        .where(
            EventoEstatus.ocurrido_at >= ini,
            EventoEstatus.ocurrido_at < fin,
            EventoEstatus.origen.in_([OrigenEvento.ONLINE.value, OrigenEvento.OFFLINE.value]),
        )
    ).all()
    total = len(eventos)
    mismo_dia = sum(1 for o, r, _ in eventos if to_local(o).date() == to_local(r).date())
    offline = sum(1 for _, _, origen in eventos if origen == OrigenEvento.OFFLINE.value)

    filas = db.execute(
        scope.restringir(select(Factura.estatus, func.count(Factura.id)).group_by(Factura.estatus), Factura)
    ).all()
    por_estatus: dict[str, int] = {str(est): int(n) for est, n in filas}
    consultas_total = db.scalar(
        select(func.count(ConsultaPublica.id)).where(
            ConsultaPublica.created_at >= ini, ConsultaPublica.created_at < fin
        )
    ) or 0
    consultas_ok = db.scalar(
        select(func.count(ConsultaPublica.id)).where(
            ConsultaPublica.created_at >= ini, ConsultaPublica.created_at < fin, ConsultaPublica.encontrado
        )
    ) or 0
    return {
        "desde": desde.isoformat(),
        "hasta": hasta.isoformat(),
        "cambios_de_estatus": total,
        "pct_mismo_dia": round(100.0 * mismo_dia / total, 1) if total else None,
        "meta_pct_mismo_dia": 95,
        "cambios_sin_senal_sincronizados": offline,
        "facturas_por_estatus": {Estatus(k).label: v for k, v in por_estatus.items()},
        "consultas_publicas": int(consultas_total),
        "consultas_publicas_encontradas": int(consultas_ok),
    }
