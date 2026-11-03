"""Servicio del catálogo de Empresas de Transporte (inquilinos).

La autorización (solo SUPERVISOR) se resuelve en el endpoint con ``Permiso.EMPRESAS_GESTIONAR``;
aquí viven las reglas de datos: unicidad de nombre y RIF, borrado seguro y auditoría.
No hace commit: el llamador controla la transacción.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from tracking.enums import AccionAuditoria
from tracking.models import EmpresaTransporte, Factura, Importacion, Remesa, Usuario
from tracking.services import audit
from tracking.services.users import ConflictoDatos

_RIF_RE = re.compile(r"^[VEJPGC]-?\d{5,9}(-?\d)?$")
_CAMPOS = ("nombre", "rif", "telefono", "email", "direccion", "activo")


class EmpresaNoEncontrada(LookupError):
    pass


class EmpresaInvalida(ValueError):
    pass


@dataclass(frozen=True)
class EmpresaConConteos:
    empresa: EmpresaTransporte
    usuarios: int
    remesas: int


def normalizar_rif(rif: str | None) -> str | None:
    if rif is None:
        return None
    valor = rif.strip().upper().replace(" ", "").replace(".", "")
    if not valor:
        return None
    if not _RIF_RE.match(valor):
        raise EmpresaInvalida("RIF inválido (ejemplo: J-12345678-9)")
    return valor


class ServicioEmpresas:
    def __init__(self, db: Session) -> None:
        self.db = db

    # -------------------------------------------------------------- lectura
    def listar(self, *, q: str | None = None, incluir_inactivas: bool = True) -> list[EmpresaConConteos]:
        usuarios = (
            select(Usuario.empresa_id, func.count(Usuario.id).label("n"))
            .group_by(Usuario.empresa_id)
            .subquery()
        )
        remesas = (
            select(Remesa.empresa_id, func.count(Remesa.id).label("n")).group_by(Remesa.empresa_id).subquery()
        )
        stmt = (
            select(EmpresaTransporte, func.coalesce(usuarios.c.n, 0), func.coalesce(remesas.c.n, 0))
            .outerjoin(usuarios, usuarios.c.empresa_id == EmpresaTransporte.id)
            .outerjoin(remesas, remesas.c.empresa_id == EmpresaTransporte.id)
            .order_by(EmpresaTransporte.nombre)
        )
        if not incluir_inactivas:
            stmt = stmt.where(EmpresaTransporte.activo.is_(True))
        if q and q.strip():
            term = f"%{q.strip()}%"
            stmt = stmt.where(EmpresaTransporte.nombre.ilike(term) | EmpresaTransporte.rif.ilike(term))
        return [EmpresaConConteos(e, int(nu), int(nr)) for e, nu, nr in self.db.execute(stmt)]

    def obtener(self, empresa_id: int) -> EmpresaTransporte:
        empresa = self.db.get(EmpresaTransporte, empresa_id)
        if empresa is None:
            raise EmpresaNoEncontrada("Empresa no encontrada")
        return empresa

    def con_conteos(self, empresa: EmpresaTransporte) -> EmpresaConConteos:
        nu = self.db.scalar(select(func.count(Usuario.id)).where(Usuario.empresa_id == empresa.id)) or 0
        nr = self.db.scalar(select(func.count(Remesa.id)).where(Remesa.empresa_id == empresa.id)) or 0
        return EmpresaConConteos(empresa, int(nu), int(nr))

    def asignable(self, empresa_id: int) -> EmpresaTransporte:
        """Empresa válida para asociarle usuarios o cargas (existe y está activa)."""
        empresa = self.db.get(EmpresaTransporte, empresa_id)
        if empresa is None:
            raise EmpresaNoEncontrada(f"La empresa {empresa_id} no existe")
        if not empresa.activo:
            raise EmpresaInvalida(f"La empresa «{empresa.nombre}» está inactiva")
        return empresa

    # ------------------------------------------------------------- escritura
    def crear(self, actor: Usuario, datos: dict[str, Any]) -> EmpresaTransporte:
        valores = self._normalizar({k: v for k, v in datos.items() if k in _CAMPOS})
        self._validar_unicidad(valores.get("nombre"), valores.get("rif"))
        empresa = EmpresaTransporte(**valores)
        self.db.add(empresa)
        self.db.flush()
        audit.registrar(self.db, actor, AccionAuditoria.EMPRESA_CREADA, None,
                        {"empresa_id": empresa.id, "nombre": empresa.nombre})
        return empresa

    def actualizar(self, actor: Usuario, empresa_id: int, cambios: dict[str, Any]) -> EmpresaTransporte:
        empresa = self.obtener(empresa_id)
        valores = self._normalizar({k: v for k, v in cambios.items() if k in _CAMPOS})
        self._validar_unicidad(valores.get("nombre"), valores.get("rif"), excluir_id=empresa.id)
        modificados = sorted(k for k, v in valores.items() if getattr(empresa, k) != v)
        for k, v in valores.items():
            setattr(empresa, k, v)
        if modificados:
            accion = (
                AccionAuditoria.EMPRESA_DESACTIVADA
                if "activo" in modificados and not empresa.activo
                else AccionAuditoria.EMPRESA_EDITADA
            )
            audit.registrar(self.db, actor, accion, None, {"empresa_id": empresa.id, "campos": modificados})
        return empresa

    def eliminar(self, actor: Usuario, empresa_id: int) -> bool:
        """Borra la empresa si nada la referencia; si tiene historial, la desactiva. Devuelve si se borró."""
        empresa = self.obtener(empresa_id)
        if self._tiene_referencias(empresa.id):
            if empresa.activo:
                empresa.activo = False
                audit.registrar(self.db, actor, AccionAuditoria.EMPRESA_DESACTIVADA, None,
                                {"empresa_id": empresa.id, "motivo": "eliminacion_con_historial"})
            return False
        audit.registrar(self.db, actor, AccionAuditoria.EMPRESA_ELIMINADA, None,
                        {"empresa_id": empresa.id, "nombre": empresa.nombre})
        self.db.delete(empresa)
        return True

    # ------------------------------------------------------------- internos
    @staticmethod
    def _normalizar(valores: dict[str, Any]) -> dict[str, Any]:
        out = dict(valores)
        if "nombre" in out:
            nombre = " ".join(str(out["nombre"] or "").split())
            if len(nombre) < 2:
                raise EmpresaInvalida("Indique el nombre de la empresa")
            out["nombre"] = nombre
        if "rif" in out:
            out["rif"] = normalizar_rif(out["rif"])
        for k in ("telefono", "email", "direccion"):
            if k in out:
                out[k] = (str(out[k]).strip() or None) if out[k] is not None else None
        if "activo" in out and out["activo"] is None:
            del out["activo"]
        return out

    def _validar_unicidad(self, nombre: str | None, rif: str | None, excluir_id: int | None = None) -> None:
        def _existe(cond: Any) -> bool:
            stmt = select(EmpresaTransporte.id).where(cond)
            if excluir_id is not None:
                stmt = stmt.where(EmpresaTransporte.id != excluir_id)
            return self.db.scalar(stmt) is not None

        if nombre and _existe(func.lower(EmpresaTransporte.nombre) == nombre.lower()):
            raise ConflictoDatos(f"Ya existe una empresa llamada «{nombre}»")
        if rif and _existe(EmpresaTransporte.rif == rif):
            raise ConflictoDatos(f"Ya existe una empresa con RIF {rif}")

    def _tiene_referencias(self, empresa_id: int) -> bool:
        return bool(
            self.db.scalar(
                select(
                    exists().where(Usuario.empresa_id == empresa_id)
                    | exists().where(Remesa.empresa_id == empresa_id)
                    | exists().where(Factura.empresa_id == empresa_id)
                    | exists().where(Importacion.empresa_id == empresa_id)
                )
            )
        )
