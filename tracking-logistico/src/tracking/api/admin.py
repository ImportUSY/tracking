"""Administración: conductores, camiones, usuarios, vendedores y carga de catálogos."""

from __future__ import annotations

import io
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response
from pydantic import ValidationError
from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from tracking.api.importaciones import leer_excel, leer_json
from tracking.config import Settings, get_settings
from tracking.enums import AccionAuditoria, AccionBitacora, Rol
from tracking.models import Auditoria, Bitacora, BitacoraArchivo, Camion, Conductor, Factura, Remesa, Usuario, Vendedor
from tracking.permissions import (
    Permiso,
    PermisoDenegado,
    puede_ver_datos_personales,
    require_permission,
    validar_cambio_activo,
)
from tracking.repositories import ConductorRepositorio, get_conductores
from tracking.schemas import (
    ActivoIn,
    ArchivarBitacoraIn,
    AuditoriaOut,
    BitacoraArchivoOut,
    BitacoraOut,
    CamionIn,
    CamionOut,
    CamionUpdate,
    ConductorIn,
    ConductorOut,
    ConductorUpdate,
    CredencialTemporal,
    EstablecerClaveIn,
    EstadoArchivoBitacoraOut,
    EstadoCredencialOut,
    FilasExcelIn,
    PaginaBitacora,
    PaginaConsumo,
    ResultadoClaveOut,
    UsuarioIn,
    UsuarioOut,
    UsuarioUpdate,
    VendedorIn,
    VendedorOut,
    VendedorUpdate,
)
from tracking.security import get_db
from tracking.services import audit, consumo_datos
from tracking.services import bitacora_archivo as archivo_bitacora
from tracking.services.accounts import ClaveInvalida, DatosInvalidos, ServicioCuentas
from tracking.services.catalogos import (
    ResultadoCatalogo,
    importar_placas,
    importar_placas_filas,
    importar_vendedores,
)
from tracking.services.normalize import clean_code, clean_phone
from tracking.services.reportes import validar_rango
from tracking.services.timeutil import local_day_bounds
from tracking.services.users import ConflictoDatos
from tracking.tenancy import ROLES_AISLADOS, TODAS_LAS_EMPRESAS, TenantScope

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _conflict(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


def _cuentas(db: Session, settings: Settings) -> ServicioCuentas:
    return ServicioCuentas(db, settings)


def _http(exc: Exception) -> HTTPException:
    """Traduce errores de negocio del servicio de cuentas a respuestas HTTP."""
    if isinstance(exc, PermisoDenegado):
        return HTTPException(status.HTTP_403_FORBIDDEN, exc.mensaje)
    if isinstance(exc, ConflictoDatos):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, (ClaveInvalida, DatosInvalidos)):
        return HTTPException(422, str(exc))
    raise exc


_ERRORES_CUENTA = (PermisoDenegado, ConflictoDatos, ClaveInvalida, DatosInvalidos)


# ------------------------------------------------------------------ Conductores
def _conductor_out(c: Conductor, viewer: Usuario) -> ConductorOut:
    out = ConductorOut.model_validate(c)
    out.empresa_id = ConductorRepositorio.empresa_de(c)
    if not puede_ver_datos_personales(viewer):
        out.cedula = None
        out.telefono = None
    return out


@router.get("/conductores", response_model=list[ConductorOut])
def listar_conductores(
    q: str | None = None,
    incluir_inactivos: bool = False,
    user: Usuario = Depends(require_permission(Permiso.CATALOGOS_VER)),
    conductores: ConductorRepositorio = Depends(get_conductores),
) -> list[ConductorOut]:
    """Transportista: solo los conductores de su empresa. Supervisor/Cobranzas: todos."""
    stmt = conductores.consulta().order_by(Conductor.nombre)
    if not incluir_inactivos:
        stmt = stmt.where(Conductor.activo.is_(True))
    if q:
        stmt = stmt.where(
            or_(Conductor.nombre.ilike(f"%{q}%"), Conductor.cedula.ilike(f"%{q}%"), Conductor.placa.ilike(f"%{q}%"))
        )
    return [_conductor_out(c, user) for c in conductores.db.scalars(stmt)]


@router.post("/conductores", status_code=status.HTTP_201_CREATED)
def crear_conductor(
    body: ConductorIn,
    user: Usuario = Depends(require_permission(Permiso.CONDUCTORES_CREAR)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Alta de conductor (y su usuario). Solo roles en CONDUCTOR_CREATOR_ROLES."""
    clave = body.clave.get_secret_value() if body.clave else None
    try:
        c, temporal = _cuentas(db, settings).crear_conductor(
            user, cedula=body.cedula, nombre=body.nombre, telefono=body.telefono, placa=body.placa, clave=clave,
            empresa_id=body.empresa_id,
        )
    except _ERRORES_CUENTA as exc:
        db.rollback()
        raise _http(exc) from exc
    c.activo = body.activo
    db.commit()
    cred = CredencialTemporal(usuario=c.cedula, nombre=c.nombre, clave_temporal=temporal) if temporal else None
    return {"conductor": _conductor_out(c, user), "credencial": cred}


@router.patch("/conductores/{conductor_id}", response_model=ConductorOut)
def editar_conductor(
    conductor_id: int,
    body: ConductorUpdate,
    user: Usuario = Depends(require_permission(Permiso.CATALOGOS_GESTIONAR)),
    db: Session = Depends(get_db),
    conductores: ConductorRepositorio = Depends(get_conductores),
) -> ConductorOut:
    c = conductores.obtener(conductor_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conductor no encontrado")
    data = body.model_dump(exclude_unset=True)
    if "nombre" in data:
        c.nombre = data["nombre"].strip()
        if c.usuario:
            c.usuario.nombre = c.nombre
    if "telefono" in data:
        c.telefono = clean_phone(data["telefono"])
    if "placa" in data:
        c.placa = clean_code(data["placa"], 20)
    if "activo" in data:
        c.activo = data["activo"]
        if c.usuario:
            c.usuario.activo = data["activo"]
            c.usuario.token_version += 1
    db.commit()
    return _conductor_out(c, user)


@router.delete("/conductores/{conductor_id}")
def eliminar_conductor(
    conductor_id: int,
    _: Usuario = Depends(require_permission(Permiso.CATALOGOS_GESTIONAR)),
    db: Session = Depends(get_db),
    conductores: ConductorRepositorio = Depends(get_conductores),
) -> dict[str, object]:
    """Si tiene remesas se desactiva (se conserva el historial); si no, se elimina."""
    c = conductores.obtener(conductor_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conductor no encontrado")
    # Comprobación de sistema: el historial puede estar en remesas de otra empresa (reasignación)
    tiene_historial = db.scalar(
        select(exists().where(Remesa.conductor_id == c.id)).execution_options(**TODAS_LAS_EMPRESAS)
    )
    usuario = c.usuario
    if tiene_historial:
        c.activo = False
        if usuario:
            usuario.activo = False
            usuario.token_version += 1
        db.commit()
        return {"eliminado": False, "desactivado": True}
    db.delete(c)
    if usuario:
        db.delete(usuario)
    db.commit()
    return {"eliminado": True, "desactivado": False}


@router.post("/conductores/{conductor_id}/clave", response_model=ResultadoClaveOut)
def clave_conductor(
    conductor_id: int,
    body: EstablecerClaveIn | None = None,
    actor: Usuario = Depends(require_permission(Permiso.CLAVES_CONDUCTORES_GESTIONAR)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    conductores: ConductorRepositorio = Depends(get_conductores),
) -> ResultadoClaveOut:
    """Fija la clave de un conductor o, sin cuerpo, genera una temporal (Supervisor y Transportista)."""
    c = conductores.obtener(conductor_id)
    if c is None or c.usuario is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conductor sin usuario")
    return _aplicar_clave(db, settings, actor, c.usuario, body)


def _aplicar_clave(
    db: Session, settings: Settings, actor: Usuario, objetivo: Usuario, body: EstablecerClaveIn | None
) -> ResultadoClaveOut:
    body = body or EstablecerClaveIn()
    nueva = body.clave_nueva.get_secret_value() if body.clave_nueva else None
    try:
        r = _cuentas(db, settings).establecer_clave(actor, objetivo, nueva, body.debe_cambiar_clave)
    except (PermisoDenegado, ClaveInvalida) as exc:
        db.rollback()
        raise _http(exc) from exc
    db.commit()
    return ResultadoClaveOut(
        usuario_id=objetivo.id,
        usuario=objetivo.username,
        nombre=objetivo.nombre,
        debe_cambiar_clave=objetivo.debe_cambiar_clave,
        clave_temporal=r.clave_temporal,
    )


# --------------------------------------------------------------------- Camiones
@router.get("/camiones", response_model=list[CamionOut])
def listar_camiones(
    _: Usuario = Depends(require_permission(Permiso.CATALOGOS_VER)), db: Session = Depends(get_db)
) -> list[Camion]:
    return list(db.scalars(select(Camion).order_by(Camion.placa)))


@router.post("/camiones", response_model=CamionOut, status_code=status.HTTP_201_CREATED)
def crear_camion(
    body: CamionIn, _: Usuario = Depends(require_permission(Permiso.CATALOGOS_GESTIONAR)), db: Session = Depends(get_db)
) -> Camion:
    placa = clean_code(body.placa, 20)
    if not placa:
        raise HTTPException(422, "Placa inválida")
    if db.scalar(select(Camion.id).where(Camion.placa == placa)):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Ya existe el camión {placa}")
    cam = Camion(
        placa=placa, color=body.color, serial=clean_code(body.serial, 60), clase=body.clase, activo=body.activo
    )
    db.add(cam)
    db.commit()
    return cam


@router.patch("/camiones/{camion_id}", response_model=CamionOut)
def editar_camion(
    camion_id: int,
    body: CamionUpdate,
    _: Usuario = Depends(require_permission(Permiso.CATALOGOS_GESTIONAR)),
    db: Session = Depends(get_db),
) -> Camion:
    cam = db.get(Camion, camion_id)
    if cam is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Camión no encontrado")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(cam, k, clean_code(v, 60) if k == "serial" else v)
    db.commit()
    return cam


@router.delete("/camiones/{camion_id}", status_code=status.HTTP_204_NO_CONTENT)
def eliminar_camion(
    camion_id: int, _: Usuario = Depends(require_permission(Permiso.CATALOGOS_GESTIONAR)), db: Session = Depends(get_db)
) -> None:
    cam = db.get(Camion, camion_id)
    if cam is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Camión no encontrado")
    db.delete(cam)  # las remesas guardan la placa como texto: el historial no se pierde
    db.commit()


# --------------------------------------------------------------------- Usuarios
def _user_out(u: Usuario) -> UsuarioOut:
    out = UsuarioOut.model_validate(u)
    out.conductor_id = u.conductor.id if u.conductor else None
    out.empresa_nombre = u.empresa.nombre if u.empresa else None
    return out


def _usuario_o_404(db: Session, usuario_id: int, actor: Usuario | None = None) -> Usuario:
    """Usuario por id; para un actor restringido, uno de OTRA empresa se trata como inexistente."""
    u = db.get(Usuario, usuario_id)
    ajeno = (
        actor is not None and u is not None and u.empresa_id is not None
        and not TenantScope.de_usuario(actor).permite(u.empresa_id)
    )
    if u is None or ajeno:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Usuario no encontrado")
    return u


@router.get("/usuarios", response_model=list[UsuarioOut])
def listar_usuarios(
    incluir_conductores: bool = False,
    empresa_id: int | None = None,
    actor: Usuario = Depends(require_permission(Permiso.USUARIOS_VER)),
    db: Session = Depends(get_db),
) -> list[UsuarioOut]:
    stmt = select(Usuario).order_by(Usuario.rol, Usuario.nombre)
    scope = TenantScope.de_usuario(actor)
    if scope.restringido:
        # Transportista: solo los CONDUCTORES de su propia empresa
        stmt = stmt.where(Usuario.rol == Rol.CONDUCTOR.value, Usuario.empresa_id == scope.empresa_id)
        return [_user_out(u) for u in db.scalars(stmt)]
    if empresa_id is not None:
        stmt = stmt.where(Usuario.empresa_id == empresa_id)
    if not incluir_conductores:
        stmt = stmt.where(Usuario.rol != Rol.CONDUCTOR.value)
    return [_user_out(u) for u in db.scalars(stmt)]


@router.post("/usuarios/{usuario_id}/activo", response_model=UsuarioOut)
def cambiar_activo(
    usuario_id: int,
    body: ActivoIn,
    actor: Usuario = Depends(require_permission(Permiso.USUARIOS_ACTIVAR)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> UsuarioOut:
    """Activo Sí/No. Con «No» el usuario pierde el acceso al instante (se cierran sus sesiones y el login
    se rechaza) hasta que vuelva a «Sí». Supervisor: cualquiera; Transportista: conductores de su empresa."""
    u = _usuario_o_404(db, usuario_id, actor)
    try:
        validar_cambio_activo(actor, u, settings)
    except PermisoDenegado as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, exc.mensaje) from exc
    if u.activo != body.activo:
        u.activo = body.activo
        u.token_version += 1  # invalida los tokens vigentes: sin acceso desde la siguiente petición
        if u.conductor is not None:
            u.conductor.activo = body.activo  # la ficha del conductor sigue al usuario
        accion = AccionAuditoria.USUARIO_ACTIVADO if body.activo else AccionAuditoria.USUARIO_DESACTIVADO
        audit.registrar(db, actor, accion, u, {"usuario": u.username, "rol": u.rol})
        db.commit()
    return _user_out(u)


@router.post("/usuarios", status_code=status.HTTP_201_CREATED)
def crear_usuario_api(
    body: UsuarioIn,
    actor: Usuario = Depends(
        require_permission(Permiso.USUARIOS_GESTIONAR, Permiso.CONDUCTORES_CREAR, cualquiera=True)
    ),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Alta de usuario con jerarquía de creación.

    * rol CONDUCTOR: roles de CONDUCTOR_CREATOR_ROLES (por defecto Supervisor y Transportista).
    * rol SUPERVISOR / TRANSPORTISTA / COBRANZAS / CONSULTOR: solo Supervisor.
    """
    clave = body.clave.get_secret_value() if body.clave else None
    try:
        u, temporal = _cuentas(db, settings).crear_usuario(
            actor,
            rol=body.rol,
            nombre=body.nombre,
            username=body.username,
            email=body.email,
            clave=clave,
            cedula=body.cedula,
            telefono=body.telefono,
            placa=body.placa,
            empresa_id=body.empresa_id,
        )
    except _ERRORES_CUENTA as exc:
        db.rollback()
        raise _http(exc) from exc
    db.commit()
    cred = CredencialTemporal(usuario=u.username, nombre=u.nombre, clave_temporal=temporal) if temporal else None
    return {"usuario": _user_out(u), "credencial": cred}


@router.patch("/usuarios/{usuario_id}", response_model=UsuarioOut)
def editar_usuario(
    usuario_id: int,
    body: UsuarioUpdate,
    actor: Usuario = Depends(require_permission(Permiso.USUARIOS_GESTIONAR)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> UsuarioOut:
    u = _usuario_o_404(db, usuario_id)
    data = body.model_dump(exclude_unset=True)
    if u.id == actor.id and (data.get("activo") is False or data.get("rol", Rol.SUPERVISOR.value) != "supervisor"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No puede quitarse a sí mismo el rol o desactivarse")
    if u.rol == Rol.CONDUCTOR.value and "rol" in data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El rol de un conductor no se cambia aquí")
    # Empresa: se valida contra el rol FINAL (un TRANSPORTISTA/CONDUCTOR no puede quedar sin empresa)
    pide_empresa = "empresa_id" in data
    empresa_final = data.pop("empresa_id", u.empresa_id)
    cambios = {k: v for k, v in data.items() if getattr(u, k) != v}
    rol_final = Rol(data.get("rol") or u.rol)
    if rol_final in ROLES_AISLADOS and empresa_final is None:
        raise HTTPException(422, f"Indique la empresa de transporte del usuario {rol_final.value.upper()}")
    if pide_empresa or rol_final.value != u.rol:
        u.rol = rol_final.value  # la empresa se valida contra el rol final
        try:
            if _cuentas(db, settings).cambiar_empresa(actor, u, empresa_final):
                cambios["empresa_id"] = u.empresa_id
        except _ERRORES_CUENTA as exc:
            db.rollback()
            raise _http(exc) from exc
    for k, v in data.items():
        setattr(u, k, v)
    if "activo" in data and u.conductor is not None:
        u.conductor.activo = data["activo"]  # la ficha del conductor sigue al usuario
    if "activo" in cambios or "rol" in cambios:
        u.token_version += 1  # el nuevo rol/estado rige desde ya: cierra sesiones abiertas
    if cambios:
        audit.registrar(db, actor, AccionAuditoria.USUARIO_EDITADO, u, {"campos": sorted(cambios)})
    db.commit()
    return _user_out(u)


@router.delete("/usuarios/{usuario_id}")
def eliminar_usuario(
    usuario_id: int,
    actor: Usuario = Depends(require_permission(Permiso.USUARIOS_GESTIONAR)),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    u = _usuario_o_404(db, usuario_id)
    if u.id == actor.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No puede eliminarse a sí mismo")
    if u.conductor is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Elimine al conductor desde la sección Conductores")
    audit.registrar(
        db, actor, AccionAuditoria.USUARIO_ELIMINADO, None, {"usuario": u.username, "rol": u.rol, "usuario_id": u.id}
    )
    # Referencias de trazabilidad: en SQLite la FK no aplica ON DELETE SET NULL
    db.execute(update(Usuario).where(Usuario.clave_actualizada_por_id == u.id).values(clave_actualizada_por_id=None))
    db.delete(u)  # eventos/importaciones/auditoría quedan con usuario NULL (historial intacto)
    db.commit()
    return {"eliminado": True}


@router.post("/usuarios/{usuario_id}/clave", response_model=ResultadoClaveOut)
def clave_usuario(
    usuario_id: int,
    body: EstablecerClaveIn | None = None,
    actor: Usuario = Depends(
        require_permission(Permiso.CLAVES_USUARIOS_GESTIONAR, Permiso.CLAVES_CONDUCTORES_GESTIONAR, cualquiera=True)
    ),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ResultadoClaveOut:
    """Edita la clave de un usuario.

    * Con ``clave_nueva``: la fija (y por defecto obliga a cambiarla al entrar).
    * Sin cuerpo: genera una clave temporal que se muestra UNA sola vez.
    SUPERVISOR: cualquier usuario. TRANSPORTISTA: solo conductores. Cierra las sesiones del usuario.
    """
    return _aplicar_clave(db, settings, actor, _usuario_o_404(db, usuario_id, actor), body)


@router.get("/usuarios/{usuario_id}/credencial", response_model=EstadoCredencialOut)
def estado_credencial(
    usuario_id: int,
    actor: Usuario = Depends(require_permission(Permiso.CREDENCIALES_CONSULTAR)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> EstadoCredencialOut:
    """Consulta administrativa de la credencial. La clave no es recuperable (hash Argon2id)."""
    try:
        datos = _cuentas(db, settings).estado_credencial(actor, _usuario_o_404(db, usuario_id))
    except PermisoDenegado as exc:
        raise _http(exc) from exc
    db.commit()
    return EstadoCredencialOut.model_validate(datos)


@router.get("/auditoria", response_model=list[AuditoriaOut])
def ver_auditoria(
    accion: AccionAuditoria | None = None,
    usuario_id: int | None = None,
    limite: int = Query(100, ge=1, le=500),
    _: Usuario = Depends(require_permission(Permiso.AUDITORIA_VER)),
    db: Session = Depends(get_db),
) -> list[Auditoria]:
    stmt = select(Auditoria).order_by(Auditoria.id.desc()).limit(limite)
    if accion:
        stmt = stmt.where(Auditoria.accion == accion.value)
    if usuario_id:
        stmt = stmt.where(or_(Auditoria.objetivo_usuario_id == usuario_id, Auditoria.actor_id == usuario_id))
    return list(db.scalars(stmt))


# ------------------------------------------------------------------- Bitácora de actividad
@router.get("/bitacora", response_model=PaginaBitacora)
def ver_bitacora(
    desde: date | None = None,
    hasta: date | None = None,
    usuario_id: int | None = None,
    rol: str | None = Query(default=None, max_length=20),
    empresa_id: int | None = None,
    accion: AccionBitacora | None = None,
    modulo: str | None = Query(default=None, max_length=40),
    exito: bool | None = None,
    q: str | None = Query(default=None, max_length=80, description="Texto en usuario, nombre o ruta"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _: Usuario = Depends(require_permission(Permiso.AUDITORIA_VER)),
    db: Session = Depends(get_db),
) -> PaginaBitacora:
    """Bitácora de actividad (solo lectura: no existe forma de editarla ni borrarla)."""
    stmt = select(Bitacora)
    ini, fin = local_day_bounds(*validar_rango(desde, hasta)) if (desde or hasta) else (None, None)
    if ini is not None and fin is not None:
        stmt = stmt.where(Bitacora.ocurrido_at >= ini, Bitacora.ocurrido_at < fin)
    if usuario_id is not None:
        stmt = stmt.where(Bitacora.usuario_id == usuario_id)
    if rol:
        stmt = stmt.where(Bitacora.rol == rol)
    if empresa_id is not None:
        stmt = stmt.where(Bitacora.empresa_id == empresa_id)
    if accion is not None:
        stmt = stmt.where(Bitacora.accion == accion.value)
    if modulo:
        stmt = stmt.where(Bitacora.modulo == modulo)
    if exito is not None:
        stmt = stmt.where(Bitacora.exito.is_(exito))
    if q and q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(Bitacora.usuario.ilike(like), Bitacora.usuario_nombre.ilike(like), Bitacora.ruta.ilike(like))
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    filas = db.scalars(stmt.order_by(Bitacora.id.desc()).limit(limit).offset(offset))
    archivados = select(BitacoraArchivo.periodo).order_by(BitacoraArchivo.desde)
    if ini is not None and fin is not None:
        archivados = archivados.where(BitacoraArchivo.desde < fin, BitacoraArchivo.hasta > ini)
    return PaginaBitacora(total=int(total), items=[BitacoraOut.model_validate(b) for b in filas],
                          archivados=list(db.scalars(archivados)))


# ------------------------------------------------------------------- Datos de navegación
@router.get("/consumo-datos", response_model=PaginaConsumo)
def ver_consumo_datos(
    desde: date | None = None,
    hasta: date | None = None,
    usuario_id: int | None = None,
    empresa_id: int | None = None,
    ruta: str | None = Query(default=None, max_length=200),
    conexion_id: str | None = Query(default=None, max_length=36),
    q: str | None = Query(default=None, max_length=80, description="Texto en usuario, nombre o ruta"),
    agrupar: str = Query("detalle", description="detalle | conductor | dia | ruta | conexion"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _: Usuario = Depends(require_permission(Permiso.AUDITORIA_VER)),
    db: Session = Depends(get_db),
) -> PaginaConsumo:
    """Datos móviles (red celular) que usó la app de los conductores, con los mismos filtros de la bitácora."""
    filtros = consumo_datos.FiltrosConsumo(desde=desde, hasta=hasta, usuario_id=usuario_id, empresa_id=empresa_id,
                                           ruta=ruta, conexion_id=conexion_id, q=q)
    try:
        return PaginaConsumo.model_validate(consumo_datos.consultar(db, filtros, agrupar, limit, offset))
    except consumo_datos.FiltroConsumoInvalido as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


# --- Archivo de la bitácora (meses o años cerrados) -------------------------------------------------
@router.get("/bitacora/archivos", response_model=EstadoArchivoBitacoraOut)
def estado_archivo_bitacora(
    _: Usuario = Depends(require_permission(Permiso.AUDITORIA_VER)),
    db: Session = Depends(get_db),
) -> EstadoArchivoBitacoraOut:
    """Archivos existentes, períodos cerrados pendientes de archivar y tamaño de la bitácora activa."""
    archivos = db.scalars(select(BitacoraArchivo).order_by(BitacoraArchivo.desde.desc()))
    return EstadoArchivoBitacoraOut(
        registros_activos=archivo_bitacora.total_activo(db),
        pendientes=archivo_bitacora.pendientes(db),
        archivos=[BitacoraArchivoOut.model_validate(a) for a in archivos],
    )


@router.post("/bitacora/archivar", response_model=BitacoraArchivoOut, status_code=status.HTTP_201_CREATED)
def archivar_bitacora(
    body: ArchivarBitacoraIn,
    actor: Usuario = Depends(require_permission(Permiso.BITACORA_ARCHIVAR)),
    db: Session = Depends(get_db),
) -> BitacoraArchivoOut:
    """Saca un mes o un año cerrado de la bitácora activa y lo guarda comprimido (todo o nada)."""
    try:
        archivo = archivo_bitacora.archivar(db, body.tipo, body.periodo, actor)
    except archivo_bitacora.ArchivoInvalido as exc:
        db.rollback()
        codigo = 409 if "ya está archivado" in str(exc) or "ya hay meses" in str(exc).lower() else 422
        raise HTTPException(codigo, str(exc)) from exc
    audit.registrar(db, actor, AccionAuditoria.BITACORA_ARCHIVADA, None,
                    {"periodo": archivo.periodo, "filas": archivo.filas, "sha256": archivo.sha256})
    db.commit()
    return BitacoraArchivoOut.model_validate(archivo)


@router.get("/bitacora/archivos/{archivo_id}/descargar")
def descargar_archivo_bitacora(
    archivo_id: int,
    formato: str = Query("csv", pattern="^(csv|jsonl.gz)$"),
    _: Usuario = Depends(require_permission(Permiso.AUDITORIA_VER)),
    db: Session = Depends(get_db),
) -> Response:
    """CSV para Excel (fecha y hora local) o el archivo original JSON Lines + gzip (verificable con su SHA-256)."""
    archivo = db.get(BitacoraArchivo, archivo_id)
    if archivo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Archivo no encontrado")
    nombre = f"bitacora_{archivo.periodo}"
    if formato == "jsonl.gz":
        return Response(archivo.contenido, media_type="application/gzip",
                        headers={"Content-Disposition": f'attachment; filename="{nombre}.jsonl.gz"',
                                 "X-Content-SHA256": archivo.sha256})
    try:
        cuerpo = b"".join(archivo_bitacora.a_csv(archivo))
    except archivo_bitacora.ArchivoInvalido as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return Response(cuerpo, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{nombre}.csv"'})


# ------------------------------------------------------------------- Vendedores
@router.get("/vendedores", response_model=list[VendedorOut])
def listar_vendedores(
    _: Usuario = Depends(require_permission(Permiso.CATALOGOS_VER)), db: Session = Depends(get_db)
) -> list[Vendedor]:
    return list(db.scalars(select(Vendedor).order_by(Vendedor.codigo)))


@router.post("/vendedores", response_model=VendedorOut, status_code=status.HTTP_201_CREATED)
def crear_vendedor(
    body: VendedorIn,
    _: Usuario = Depends(require_permission(Permiso.CATALOGOS_GESTIONAR)),
    db: Session = Depends(get_db),
) -> Vendedor:
    codigo = clean_code(body.codigo, 20)
    if not codigo:
        raise HTTPException(422, "Código inválido")
    if db.get(Vendedor, codigo):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Ya existe el vendedor {codigo}")
    v = Vendedor(codigo=codigo, nombre=body.nombre, territorio=body.territorio, email=body.email, activo=body.activo)
    db.add(v)
    db.commit()
    return v


@router.patch("/vendedores/{codigo}", response_model=VendedorOut)
def editar_vendedor(
    codigo: str,
    body: VendedorUpdate,
    _: Usuario = Depends(require_permission(Permiso.CATALOGOS_GESTIONAR)),
    db: Session = Depends(get_db),
) -> Vendedor:
    v = db.get(Vendedor, codigo.upper())
    if v is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Vendedor no encontrado")
    for k, val in body.model_dump(exclude_unset=True).items():
        setattr(v, k, val)
    db.commit()
    return v


@router.delete("/vendedores/{codigo}", status_code=status.HTTP_204_NO_CONTENT)
def eliminar_vendedor(
    codigo: str, _: Usuario = Depends(require_permission(Permiso.CATALOGOS_GESTIONAR)), db: Session = Depends(get_db)
) -> None:
    v = db.get(Vendedor, codigo.upper())
    if v is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Vendedor no encontrado")
    en_uso = db.scalar(select(func.count(Factura.id)).where(Factura.cod_vendedor == v.codigo))
    if en_uso:
        v.activo = False  # las facturas guardan el código; se desactiva para no perder la referencia
    else:
        db.delete(v)
    db.commit()


# -------------------------------------------------------------- Carga catálogos
@router.post("/catalogos/vendedores")
async def cargar_vendedores(
    archivo: UploadFile = File(...),
    _: Usuario = Depends(require_permission(Permiso.CATALOGOS_GESTIONAR)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    data = await leer_excel(archivo, settings.max_excel_mb)
    try:
        r = await run_in_threadpool(importar_vendedores, db, io.BytesIO(data))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"creados": r.creados, "actualizados": r.actualizados, "omitidos": r.omitidos}


def _empresa_placas(db: Session, settings: Settings, actor: Usuario, empresa_id: int | None) -> int | None:
    """Empresa de los conductores nuevos de la hoja PLACAS (Transportista: la suya; Supervisor: obligatoria)."""
    try:
        return _cuentas(db, settings).resolver_empresa(actor, Rol.CONDUCTOR, empresa_id)
    except _ERRORES_CUENTA as exc:
        raise _http(exc) from exc


def _respuesta_placas(db: Session, settings: Settings, actor: Usuario, r: ResultadoCatalogo) -> dict[str, object]:
    _cuentas(db, settings).registrar_conductores_importados(actor, [c["usuario"] for c in r.credenciales])
    db.commit()
    return {
        "creados": r.creados,
        "actualizados": r.actualizados,
        "omitidos": r.omitidos,
        "credenciales": r.credenciales,
        "detalle": r.detalle[:50],
    }


@router.post("/catalogos/placas/filas")
async def cargar_placas_filas(
    request: Request,
    actor: Usuario = Depends(require_permission(Permiso.CATALOGOS_GESTIONAR, Permiso.CONDUCTORES_CREAR)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Hoja PLACAS ya leída en el navegador (evita subir el libro completo)."""
    try:
        body = FilasExcelIn.model_validate(await leer_json(request, settings.max_excel_mb))
    except ValidationError as exc:
        raise HTTPException(422, "Formato de filas inválido") from exc
    empresa = _empresa_placas(db, settings, actor, body.empresa_id)
    try:
        r = await run_in_threadpool(importar_placas_filas, db, body.filas, True, empresa)
    except ValidationError as exc:
        raise HTTPException(422, "Formato de filas inválido") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _respuesta_placas(db, settings, actor, r)


@router.post("/catalogos/placas")
async def cargar_placas(
    archivo: UploadFile = File(...),
    hoja: str = Form("PLACAS"),
    empresa_id: int | None = Form(default=None),
    actor: Usuario = Depends(require_permission(Permiso.CATALOGOS_GESTIONAR, Permiso.CONDUCTORES_CREAR)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    empresa = _empresa_placas(db, settings, actor, empresa_id)
    data = await leer_excel(archivo, settings.max_excel_mb)
    try:
        r = await run_in_threadpool(importar_placas, db, io.BytesIO(data), hoja, True, empresa)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _respuesta_placas(db, settings, actor, r)
