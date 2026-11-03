"""Carga de catálogos desde Excel: vendedores (Listado Codigo Vendedores) y choferes/camiones (hoja PLACAS).

Ambas cargas son idempotentes (upsert por clave natural) y no borran nada.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from tracking.models import Camion, Conductor, Vendedor
from tracking.services.normalize import clean_cedula, clean_code, clean_phone, clean_text, norm_key
from tracking.services.users import crear_usuario_conductor
from tracking.tenancy import scope_de_sesion

log = logging.getLogger(__name__)


@dataclass
class ResultadoCatalogo:
    creados: int = 0
    actualizados: int = 0
    omitidos: int = 0
    credenciales: list[dict[str, str]] = field(default_factory=list)
    detalle: list[str] = field(default_factory=list)


Header = list[tuple[str, int]]


def _find_header(rows: list[tuple[Any, ...]], *claves: str) -> tuple[int, Header] | None:
    for i, row in enumerate(rows[:30]):
        idx = [(norm_key(v), j) for j, v in enumerate(row) if v is not None]
        if all(any(k in h for h, _ in idx) for k in claves):
            return i, idx
    return None


def _col(idx: Header, *opciones: str) -> int | None:
    for o in opciones:
        for h, j in idx:
            if h == o or h.startswith(o):
                return j
    return None


def importar_vendedores(db: Session, source: str | Path | IO[bytes]) -> ResultadoCatalogo:
    wb = load_workbook(source, read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        rows = list(ws.iter_rows(max_col=10, values_only=True))
    finally:
        wb.close()
    found = _find_header(rows, "salesperson id") or _find_header(rows, "codigo")
    if found is None:
        raise ValueError("No se encontró el encabezado 'Salesperson ID' o 'Codigo' en el listado de vendedores")
    start, idx = found
    c_cod = _col(idx, "salesperson id", "codigo", "cod")
    c_nom = _col(idx, "salespersons last name", "nombre", "vendedor")
    c_ter = _col(idx, "sales territory", "territorio", "zona")
    c_mail = _col(idx, "email", "correo")
    res = ResultadoCatalogo()
    existentes = {v.codigo: v for v in db.scalars(select(Vendedor))}
    for row in rows[start + 1:]:
        codigo = clean_code(row[c_cod] if c_cod is not None else None, 20)
        nombre = clean_text(row[c_nom] if c_nom is not None else None, 160)
        if not codigo or not nombre:
            res.omitidos += 1
            continue
        territorio = clean_text(row[c_ter], 80) if c_ter is not None else None
        email = clean_text(row[c_mail], 200) if c_mail is not None else None
        v = existentes.get(codigo)
        if v is None:
            v = Vendedor(codigo=codigo, nombre=nombre, territorio=territorio, email=email, activo=True)
            db.add(v)
            existentes[codigo] = v
            res.creados += 1
        else:
            v.nombre, v.territorio = nombre, territorio or v.territorio
            if email:
                v.email = email
            res.actualizados += 1
    db.commit()
    log.info("vendedores_importados", extra={"creados": res.creados, "actualizados": res.actualizados})
    return res


def importar_placas(
    db: Session,
    source: str | Path | IO[bytes],
    hoja: str = "PLACAS",
    crear_usuarios: bool = True,
    empresa_id: int | None = None,
) -> ResultadoCatalogo:
    """Hoja PLACAS: columnas A-D (Cedula, Nombre, Placa, Telefono) y F-I (PLACA, COLOR, SERIAL, CLASE)."""
    wb = load_workbook(source, read_only=True, data_only=True)
    try:
        nombre_hoja = next((n for n in wb.sheetnames if norm_key(n) == norm_key(hoja)), None)
        if nombre_hoja is None:
            raise ValueError(f"El archivo no tiene la hoja '{hoja}'")
        rows = list(wb[nombre_hoja].iter_rows(max_col=12, values_only=True))
    finally:
        wb.close()
    return importar_placas_filas(db, rows, crear_usuarios, empresa_id)


def importar_placas_filas(
    db: Session, rows: Sequence[Sequence[Any]], crear_usuarios: bool = True, empresa_id: int | None = None
) -> ResultadoCatalogo:
    """Misma carga que `importar_placas`, a partir de filas ya leídas (p.ej. por el navegador).

    Multi-tenant: los conductores nuevos nacen en ``empresa_id``. Si la sesión es de un usuario
    restringido (Transportista), los conductores existentes de otra empresa no se modifican.
    """
    alcance = scope_de_sesion(db)
    filas: list[tuple[Any, ...]] = [tuple(r) for r in rows]
    found = _find_header(filas, "cedula", "nombre")
    if found is None:
        raise ValueError("No se encontró el encabezado 'Cedula / Nombre' en la hoja PLACAS")
    start, idx = found
    c_ced, c_nom = _col(idx, "cedula"), _col(idx, "nombre")
    c_pla, c_tel = _col(idx, "placa"), _col(idx, "telefono")
    # El bloque de camiones es el segundo encabezado 'placa' (columna F)
    placas_cols = sorted(j for h, j in idx if h.startswith("placa"))
    c_cam = placas_cols[1] if len(placas_cols) > 1 else None
    c_color, c_serial, c_clase = _col(idx, "color"), _col(idx, "serial"), _col(idx, "clase")

    res = ResultadoCatalogo()
    conductores = {c.cedula: c for c in db.scalars(select(Conductor))}
    camiones = {c.placa: c for c in db.scalars(select(Camion))}

    def get(row: tuple[Any, ...], col: int | None) -> Any:
        return row[col] if col is not None and col < len(row) else None

    for row in filas[start + 1:]:
        # --- Conductor
        ced = clean_cedula(get(row, c_ced))
        nom = clean_text(get(row, c_nom), 160)
        if ced and nom and len(ced) >= 5:
            placa = clean_code(get(row, c_pla), 20)
            tel = clean_phone(get(row, c_tel))
            c = conductores.get(ced)
            if c is None:
                if crear_usuarios:
                    c, clave = crear_usuario_conductor(db, cedula=ced, nombre=nom.title(), telefono=tel,
                                                       placa=placa, empresa_id=empresa_id)
                    if clave:
                        res.credenciales.append({"usuario": ced, "nombre": c.nombre, "clave_temporal": clave})
                else:
                    c = Conductor(cedula=ced, nombre=nom.title(), telefono=tel, placa=placa, activo=True)
                    db.add(c)
                conductores[ced] = c
                res.creados += 1
            elif not alcance.permite(c.usuario.empresa_id if c.usuario else None):
                res.omitidos += 1
                res.detalle.append(f"C.I. {ced}: el conductor pertenece a otra empresa; no se modifica")
            else:
                # Completar datos vacíos sin pisar ediciones hechas en el sistema
                c.telefono = c.telefono or tel
                c.placa = c.placa or placa
                res.actualizados += 1
        elif ced or nom:
            res.omitidos += 1
            res.detalle.append(f"Fila de chofer incompleta: cédula={ced} nombre={nom}")
        # --- Camión
        placa_cam = clean_code(get(row, c_cam), 20)
        if placa_cam:
            cam = camiones.get(placa_cam)
            color = clean_text(get(row, c_color), 40)
            serial = clean_code(get(row, c_serial), 60)
            clase = clean_text(get(row, c_clase), 40)
            if cam is None:
                cam = Camion(placa=placa_cam, color=color, serial=serial, clase=clase, activo=True)
                db.add(cam)
                camiones[placa_cam] = cam
                res.creados += 1
            else:
                cam.color = cam.color or color
                cam.serial = cam.serial or serial
                cam.clase = cam.clase or clase
                res.actualizados += 1
    db.commit()
    log.info("placas_importadas", extra={"creados": res.creados, "actualizados": res.actualizados})
    return res
