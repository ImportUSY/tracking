"""Días feriados: no cuentan como hábiles en los tiempos entre procesos.

Todos los roles de oficina ven la lista; solo el SUPERVISOR agrega, quita o carga los nacionales de un año.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from tracking.models import Feriado, Usuario
from tracking.permissions import Permiso, require_permission
from tracking.schemas import ORM
from tracking.security import get_db
from tracking.services.feriados import cargar_nacionales

router = APIRouter(prefix="/api/feriados", tags=["feriados"])


class FeriadoIn(BaseModel):
    fecha: date
    nombre: str = Field(min_length=2, max_length=120)

    @field_validator("nombre")
    @classmethod
    def _limpiar(cls, v: str) -> str:
        return " ".join(v.split())


class FeriadoOut(ORM):
    id: int
    fecha: date
    nombre: str
    nacional: bool


class NacionalesIn(BaseModel):
    anio: int = Field(ge=2000, le=2100)


@router.get("", response_model=list[FeriadoOut])
def listar_feriados(
    anio: int | None = Query(default=None, ge=2000, le=2100),
    _: Usuario = Depends(require_permission(Permiso.CATALOGOS_VER)),
    db: Session = Depends(get_db),
) -> list[Feriado]:
    stmt = select(Feriado).order_by(Feriado.fecha)
    if anio:
        stmt = stmt.where(Feriado.fecha.between(date(anio, 1, 1), date(anio, 12, 31)))
    return list(db.scalars(stmt))


@router.post("", response_model=FeriadoOut, status_code=status.HTTP_201_CREATED)
def agregar_feriado(
    body: FeriadoIn,
    _: Usuario = Depends(require_permission(Permiso.FERIADOS_GESTIONAR)),
    db: Session = Depends(get_db),
) -> Feriado:
    if db.scalar(select(Feriado.id).where(Feriado.fecha == body.fecha)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"El {body.fecha:%d/%m/%Y} ya está registrado como feriado")
    f = Feriado(fecha=body.fecha, nombre=body.nombre, nacional=False)
    db.add(f)
    db.commit()
    db.refresh(f)
    return f


@router.delete("/{feriado_id}", status_code=status.HTTP_204_NO_CONTENT)
def quitar_feriado(
    feriado_id: int,
    _: Usuario = Depends(require_permission(Permiso.FERIADOS_GESTIONAR)),
    db: Session = Depends(get_db),
) -> Response:
    f = db.get(Feriado, feriado_id)
    if f is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Feriado no encontrado")
    db.delete(f)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/nacionales")
def cargar_feriados_nacionales(
    body: NacionalesIn,
    _: Usuario = Depends(require_permission(Permiso.FERIADOS_GESTIONAR)),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    """Agrega los feriados nacionales de Venezuela del año (fijos, Carnaval y Semana Santa) que falten."""
    n = cargar_nacionales(db, body.anio)
    db.commit()
    return {"anio": body.anio, "agregados": n}
