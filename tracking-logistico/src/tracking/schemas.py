"""Esquemas Pydantic de entrada/salida de la API."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from tracking.enums import ESTATUS_RETIRADOS, Estatus, Rol


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------- Auth
class VistaIn(BaseModel):
    vista: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9_-]+$")
    titulo: str | None = Field(default=None, max_length=80)


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=1, max_length=200)

    @field_validator("username")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class UsuarioOut(ORM):
    id: int
    username: str
    nombre: str
    rol: str
    email: str | None = None
    activo: bool
    debe_cambiar_clave: bool
    conductor_id: int | None = None
    empresa_id: int | None = None
    empresa_nombre: str | None = None
    # Clave temporal: accesos que le quedan con ella y si ya debe cambiarla para continuar
    accesos_temporales_restantes: int | None = None
    cambio_clave_obligatorio: bool = False


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expira: datetime
    usuario: UsuarioOut


class CambioClaveIn(BaseModel):
    clave_actual: SecretStr = Field(max_length=200)
    clave_nueva: SecretStr = Field(min_length=6, max_length=200)


class BiometriaActivarIn(BaseModel):
    clave: SecretStr = Field(min_length=1, max_length=200)  # se pide la clave actual para activar la huella


class BiometriaRespuestaIn(BaseModel):
    """Respuesta del teléfono (navigator.credentials) al reto ``reto_id``."""

    reto_id: str = Field(min_length=1, max_length=36)
    credencial: dict[str, Any]


class BiometriaOut(ORM):
    id: int
    dispositivo: str | None
    created_at: datetime
    ultimo_uso_at: datetime | None


class PerfilConductorIn(BaseModel):
    nombre: str | None = Field(default=None, min_length=2, max_length=160)
    placa: str | None = Field(default=None, max_length=20)
    telefono: str | None = Field(default=None, max_length=40)


# --------------------------------------------------------------- Catálogos
class ConductorIn(BaseModel):
    cedula: str = Field(min_length=3, max_length=20)
    nombre: str = Field(min_length=2, max_length=160)
    telefono: str | None = Field(default=None, max_length=40)
    placa: str | None = Field(default=None, max_length=20)
    clave: SecretStr | None = Field(default=None, min_length=6, max_length=200)
    activo: bool = True
    # Supervisor: obligatoria. Transportista: se ignora/valida, el conductor hereda la empresa del actor.
    empresa_id: int | None = Field(default=None, ge=1)


class ConductorUpdate(BaseModel):
    nombre: str | None = Field(default=None, min_length=2, max_length=160)
    telefono: str | None = Field(default=None, max_length=40)
    placa: str | None = Field(default=None, max_length=20)
    activo: bool | None = None


class ConductorOut(ORM):
    id: int
    cedula: str | None
    nombre: str
    telefono: str | None
    placa: str | None
    activo: bool
    usuario_id: int | None
    empresa_id: int | None = None


class CamionIn(BaseModel):
    placa: str = Field(min_length=3, max_length=20)
    color: str | None = Field(default=None, max_length=40)
    serial: str | None = Field(default=None, max_length=60)
    clase: str | None = Field(default=None, max_length=40)
    activo: bool = True


class CamionUpdate(BaseModel):
    color: str | None = Field(default=None, max_length=40)
    serial: str | None = Field(default=None, max_length=60)
    clase: str | None = Field(default=None, max_length=40)
    activo: bool | None = None


class CamionOut(ORM):
    id: int
    placa: str
    color: str | None
    serial: str | None
    clase: str | None
    activo: bool


class UsuarioIn(BaseModel):
    """Alta de usuario. Para rol CONDUCTOR el usuario de acceso es la cédula."""

    username: str | None = Field(default=None, min_length=3, max_length=64)
    nombre: str = Field(min_length=2, max_length=160)
    rol: Rol
    email: str | None = Field(default=None, max_length=200)
    clave: SecretStr | None = Field(default=None, min_length=6, max_length=200)
    # Empresa de transporte: obligatoria para TRANSPORTISTA y CONDUCTOR (ver tracking.tenancy)
    empresa_id: int | None = Field(default=None, ge=1)
    # Solo para rol CONDUCTOR
    cedula: str | None = Field(default=None, min_length=3, max_length=20)
    telefono: str | None = Field(default=None, max_length=40)
    placa: str | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def _campos_por_rol(self) -> UsuarioIn:
        if self.rol == Rol.CONDUCTOR:
            if not self.cedula:
                raise ValueError("Para un CONDUCTOR indique la cédula (será su usuario de acceso)")
        elif not self.username:
            raise ValueError("Indique el nombre de usuario")
        return self


class UsuarioUpdate(BaseModel):
    nombre: str | None = Field(default=None, min_length=2, max_length=160)
    rol: Literal["supervisor", "transportista", "cobranzas", "consultor"] | None = None
    email: str | None = Field(default=None, max_length=200)
    activo: bool | None = None
    empresa_id: int | None = Field(default=None, ge=1)


class EstablecerClaveIn(BaseModel):
    """Clave asignada por un administrador. Sin `clave_nueva` se genera una temporal aleatoria."""

    clave_nueva: SecretStr | None = Field(default=None, min_length=6, max_length=200)
    debe_cambiar_clave: bool = Field(default=True, description="Obligar al usuario a cambiarla en su próximo ingreso")


class ResultadoClaveOut(BaseModel):
    usuario_id: int
    usuario: str
    nombre: str
    debe_cambiar_clave: bool
    # Solo presente cuando el sistema generó la clave: se muestra UNA vez y no se guarda en claro
    clave_temporal: str | None = None


class EstadoCredencialOut(BaseModel):
    """Lo que un SUPERVISOR puede consultar de una credencial. Nunca incluye la clave ni su hash."""

    usuario_id: int
    usuario: str
    nombre: str
    rol: str
    activo: bool
    tiene_clave: bool
    algoritmo: str
    debe_cambiar_clave: bool
    accesos_clave_temporal: int | None = Field(default=None, description="Ingresos ya hechos con la clave temporal")
    clave_actualizada_at: datetime | None
    clave_actualizada_por: str | None
    ultimo_acceso_at: datetime | None
    nota: str = (
        "Las claves se guardan cifradas con Argon2id (irreversible): no se pueden mostrar. "
        "Para entregar una clave al usuario, restablézcala."
    )


class AuditoriaOut(ORM):
    id: int
    created_at: datetime
    actor_id: int | None
    actor_rol: str | None
    accion: str
    objetivo_usuario_id: int | None
    detalle: dict[str, Any]


class ActivoIn(BaseModel):
    activo: bool = Field(description="Sí (true) / No (false). Con No el usuario no puede acceder")


class BitacoraOut(ORM):
    id: int
    ocurrido_at: datetime
    usuario_id: int | None
    usuario: str | None
    usuario_nombre: str | None
    rol: str | None
    empresa_id: int | None
    empresa_nombre: str | None
    accion: str
    modulo: str | None
    metodo: str | None
    ruta: str
    recurso_id: str | None
    estado_http: int | None
    exito: bool
    detalle: dict[str, Any]
    ip: str | None
    user_agent: str | None
    duracion_ms: int | None


class PaginaBitacora(BaseModel):
    total: int
    items: list[BitacoraOut]
    archivados: list[str] = Field(default_factory=list, description="Períodos archivados que cruzan el filtro")


# --------------------------------------------------------------------- Datos de navegación (consumo móvil)
class ConsumoRegistroIn(BaseModel):
    """Suma parcial medida por el teléfono (solo red celular) para una conexión, método + ruta y día."""

    dia: date
    hora: int | None = Field(default=None, ge=0, le=23, description="Hora local; si falta, la de «desde»")
    conexion_id: str = Field(min_length=8, max_length=36, pattern=r"^[0-9A-Za-z-]+$")
    metodo: str = Field(min_length=3, max_length=8, pattern=r"^[A-Za-z]+$")
    ruta: str = Field(min_length=1, max_length=500)
    tipo_efectivo: str | None = Field(default=None, max_length=8)
    solicitudes: int = Field(ge=0, le=100_000)
    bytes_subida: int = Field(ge=0, le=2_000_000_000)
    bytes_bajada: int = Field(ge=0, le=2_000_000_000)
    desde: datetime
    hasta: datetime

    @field_validator("ruta")
    @classmethod
    def _ruta(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("La ruta debe empezar con /")
        return v


class LoteConsumoIn(BaseModel):
    registros: list[ConsumoRegistroIn] = Field(max_length=300)


class ConsumoFilaOut(BaseModel):
    dia: date | None = None
    hora: int | None = None
    usuario_id: int | None = None
    usuario: str | None = None
    usuario_nombre: str | None = None
    empresa_nombre: str | None = None
    conexion_id: str | None = None
    metodo: str | None = None
    ruta: str | None = None
    tipo_efectivo: str | None = None
    conexiones: int = 0
    conductores: int = 0
    solicitudes: int = 0
    bytes_subida: int = 0
    bytes_bajada: int = 0
    bytes_total: int = 0
    desde: datetime | None = None
    hasta: datetime | None = None


class TotalesConsumoOut(BaseModel):
    solicitudes: int
    bytes_subida: int
    bytes_bajada: int
    bytes_total: int
    conexiones: int
    conductores: int


class PaginaConsumo(BaseModel):
    agrupar: str
    total: int
    items: list[ConsumoFilaOut]
    totales: TotalesConsumoOut


class ArchivarBitacoraIn(BaseModel):
    tipo: Literal["mes", "anio"]
    periodo: str = Field(min_length=4, max_length=7, description="AAAA-MM para un mes, AAAA para un año")


class BitacoraArchivoOut(ORM):
    id: int
    tipo: str
    periodo: str
    desde: datetime
    hasta: datetime
    filas: int
    primer_id: int | None
    ultimo_id: int | None
    bytes: int
    sha256: str
    formato: str
    creado_por: str | None
    created_at: datetime


class EstadoArchivoBitacoraOut(BaseModel):
    registros_activos: int
    pendientes: list[dict[str, Any]]
    archivos: list[BitacoraArchivoOut]


class PermisosOut(BaseModel):
    rol: str
    permisos: list[str]
    permisos_interfaz: list[str] = Field(default_factory=list, description="Qué botones mostrar")
    solo_consulta: bool = False


# ---------------------------------------------------------------- Empresas
class _EmpresaCampos(BaseModel):
    telefono: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=200)
    direccion: str | None = Field(default=None, max_length=500)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str | None) -> str | None:
        v = (v or "").strip()
        if v and ("@" not in v or v.startswith("@") or v.endswith("@")):
            raise ValueError("Correo inválido")
        return v or None


class EmpresaIn(_EmpresaCampos):
    nombre: str = Field(min_length=2, max_length=160)
    rif: str | None = Field(default=None, min_length=5, max_length=20, description="RIF, p. ej. J-12345678-9")
    activo: bool = True


class EmpresaUpdate(_EmpresaCampos):
    nombre: str | None = Field(default=None, min_length=2, max_length=160)
    rif: str | None = Field(default=None, min_length=5, max_length=20)
    activo: bool | None = None


class EmpresaOut(ORM):
    id: int
    nombre: str
    rif: str | None
    telefono: str | None
    email: str | None
    direccion: str | None
    activo: bool
    created_at: datetime
    updated_at: datetime
    usuarios: int = 0
    remesas: int = 0


class EliminacionEmpresaOut(BaseModel):
    eliminada: bool
    desactivada: bool
    motivo: str | None = None


class VendedorIn(BaseModel):
    codigo: str = Field(min_length=2, max_length=20)
    nombre: str = Field(min_length=2, max_length=160)
    territorio: str | None = Field(default=None, max_length=80)
    email: str | None = Field(default=None, max_length=200)
    activo: bool = True


class VendedorUpdate(BaseModel):
    nombre: str | None = Field(default=None, min_length=2, max_length=160)
    territorio: str | None = Field(default=None, max_length=80)
    email: str | None = Field(default=None, max_length=200)
    activo: bool | None = None


class VendedorOut(ORM):
    codigo: str
    nombre: str
    territorio: str | None
    email: str | None
    activo: bool


class CredencialTemporal(BaseModel):
    usuario: str
    nombre: str
    clave_temporal: str


# ------------------------------------------------------------ Importación
class FilasExcelIn(BaseModel):
    archivo: str = Field(default="remesa.xlsx", max_length=255)
    # Carga de PLACAS por un Supervisor: empresa de los conductores nuevos (el Transportista usa la suya)
    empresa_id: int | None = Field(default=None, ge=1)
    hoja: str = Field(default="", max_length=120)
    hojas: list[str] = Field(default_factory=list, max_length=200)
    sha256: str | None = Field(default=None, max_length=64)
    filas: list[list[Any]] = Field(max_length=300_000)
    # Hoja REMESA: solo se analizan las facturas con «Fecha Fac» en [desde, hasta] (vacío = toda la hoja)
    fecha_fac_desde: date | None = None
    fecha_fac_hasta: date | None = None


class ConfirmarImportacionIn(BaseModel):
    remesas: list[str] | None = Field(
        default=None, description="Números de remesa a importar. Vacío = las sugeridas (nuevas)."
    )
    fecha_desde: date | None = Field(default=None, description="Solo remesas con fecha >= esta fecha")
    registrar_choferes: bool = Field(
        default=False, description="Registrar automáticamente choferes que no existen en el sistema"
    )
    empresa_id: int | None = Field(
        default=None,
        ge=1,
        description="Supervisor: empresa de los choferes nuevos (obligatoria con registrar_choferes). "
        "El Transportista siempre carga en su propia empresa.",
    )


class ImportacionOut(ORM):
    id: int
    archivo_nombre: str
    hoja: str
    estado: str
    created_at: datetime
    confirmado_at: datetime | None
    resumen: dict[str, Any]
    resultado: dict[str, Any] | None = None
    revision_ia: str | None = None


class ResultadoConfirmacionOut(BaseModel):
    importacion_id: int
    remesas_creadas: int
    remesas_actualizadas: int
    facturas_creadas: int
    facturas_omitidas: int
    choferes_registrados: list[CredencialTemporal]
    remesas_excluidas: list[str]


# ----------------------------------------------------- Captura de facturas (OCR)
class CampoOCROut(BaseModel):
    valor: Any = None
    texto: str | None = None
    confianza: float | None = None
    dudoso: bool = True


class CapturaOut(ORM):
    id: str
    estado: str
    archivo_nombre: str
    content_type: str
    bytes: int
    motor: str
    created_at: datetime
    confirmado_at: datetime | None = None
    error: str | None = None
    avisos: list[str] = Field(default_factory=list)
    campos: dict[str, CampoOCROut] = Field(default_factory=dict, description="Lo que leyó la IA (o vacío)")
    datos_validados: dict[str, Any] | None = None
    factura_id: int | None = None
    remesa_id: int | None = None
    imagen_url: str = ""


class ZonaOCRIn(BaseModel):
    texto: str = Field(max_length=200)
    confianza: float | None = Field(default=None, ge=0, le=1)


class CajaOCRIn(BaseModel):
    """Una frase leída y su posición en la foto (píxeles de la imagen de trabajo)."""

    texto: str = Field(max_length=300)
    confianza: float = Field(ge=0, le=1)
    x: float = Field(ge=0, le=100000)
    y: float = Field(ge=0, le=100000)
    w: float = Field(ge=0, le=100000)
    h: float = Field(ge=0, le=100000)


class LecturaOCRIn(BaseModel):
    """Lo que el navegador leyó de la foto con PaddleOCR (OCR gratuito en el dispositivo)."""

    texto: str = Field(max_length=20000)
    confianza: float | None = Field(default=None, ge=0, le=1)
    zonas: dict[str, ZonaOCRIn] = Field(default_factory=dict, max_length=20)
    cajas: list[CajaOCRIn] = Field(default_factory=list, max_length=600)


class CapturaConfirmarIn(BaseModel):
    """Datos revisados por el operador + asignación de conductor y remesa."""

    numero: str = Field(min_length=1, max_length=40)
    cliente: str = Field(min_length=2, max_length=255)
    pedido: str | None = Field(default=None, max_length=40)
    empresa_emisora: str | None = Field(default=None, max_length=120)
    fecha_fac: date | None = None
    cod_vendedor: str | None = Field(default=None, max_length=20)
    bultos: int | None = Field(default=None, ge=0, le=100_000)
    total_fact: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=4)
    porcentaje: Decimal | None = Field(default=None, ge=0, le=1, decimal_places=4,
                                       description="Descuento como fracción: 0.05 = 5 %")
    total_neto: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=4)
    comentario: str | None = Field(default=None, max_length=2000)
    # Asignación
    conductor_id: int = Field(ge=1)
    remesa_id: int | None = Field(default=None, ge=1, description="Remesa abierta del conductor; vacío = nueva")
    remesa_numero: str | None = Field(default=None, max_length=40, description="Solo para remesa nueva")
    destino: str | None = Field(default=None, max_length=160, description="Solo para remesa nueva")


class ResultadoCapturaOut(BaseModel):
    captura_id: str
    factura_id: int
    factura_numero: str
    remesa_id: int
    remesa_numero: str
    remesa_nueva: bool
    conductor: str


# ----------------------------------------------------------------- Estatus
class UbicacionIn(BaseModel):
    """Coordenadas GPS del teléfono (opcionales: si el teléfono no pudo obtenerlas no se envían)."""

    latitud: float | None = Field(default=None, ge=-90, le=90)
    longitud: float | None = Field(default=None, ge=-180, le=180)
    precision_m: float | None = Field(default=None, ge=0, le=100000)


class EventoIn(UbicacionIn):
    id: str = Field(min_length=8, max_length=36, description="UUID generado en el dispositivo")
    factura_id: int
    estatus: Estatus
    ocurrido_at: datetime | None = None
    motivo: str | None = Field(default=None, max_length=1000)
    foto_id: str | None = Field(default=None, max_length=36)
    offline: bool = False

    @field_validator("estatus", mode="before")
    @classmethod
    def _estatus_retirado(cls, v: object) -> object:
        """Un «Cargado» guardado sin señal por la versión anterior de la app se registra como «Por Entregar»."""
        return ESTATUS_RETIRADOS.get(v, v) if isinstance(v, str) else v


class EventoOperativoIn(UbicacionIn):
    """Cuerpo de POST /api/conductor/facturas/{id}/{tipo}: el estatus lo determina el tipo de evento."""

    id: str = Field(min_length=8, max_length=36, description="UUID generado en el dispositivo")
    ocurrido_at: datetime | None = None
    motivo: str | None = Field(default=None, max_length=1000)
    foto_id: str | None = Field(default=None, max_length=36)
    offline: bool = False


class LoteEventosIn(BaseModel):
    eventos: list[EventoIn] = Field(min_length=1, max_length=500)


class EventoResultado(BaseModel):
    id: str
    ok: bool
    aplicado: bool = False
    duplicado: bool = False
    factura_id: int
    estatus_actual: str | None = None
    estatus_at: datetime | None = None
    tracking: str | None = None
    error: str | None = None
    codigo_error: Literal["conflicto", "foto_pendiente", "no_encontrada", "sin_permiso", "invalido"] | None = None


class LoteEventosOut(BaseModel):
    resultados: list[EventoResultado]
    servidor_at: datetime


class CambioEstatusAdminIn(BaseModel):
    estatus: Estatus
    motivo: str | None = Field(default=None, max_length=1000)
    ocurrido_at: datetime | None = Field(
        default=None, description="Fecha y hora de recepción (obligatoria al pasar a Entregada)")
    foto_id: str | None = Field(default=None, max_length=36, description="Foto ya subida con PUT /api/fotos/{id}")
    foto_ids: list[str] = Field(default_factory=list, max_length=10,
                                description="Fotos adicionales del mismo cambio (ya subidas)")


class AdjuntarFotoIn(BaseModel):
    foto_id: str | None = Field(default=None, min_length=8, max_length=36,
                                description="Foto ya subida con PUT /api/fotos/{id}")
    foto_ids: list[str] = Field(default_factory=list, max_length=10, description="Varias fotos ya subidas")

    def todas(self) -> list[str]:
        ids = ([self.foto_id] if self.foto_id else []) + self.foto_ids
        return list(dict.fromkeys(x.lower() for x in ids))


class FotoEventoOut(BaseModel):
    id: str
    capturada_at: datetime | None = Field(default=None, description="Fecha y hora en que se tomó la foto")


class ReasignarRemesaIn(BaseModel):
    conductor_id: int


class CambiarEmpresaTransporteIn(BaseModel):
    """Solo SUPERVISOR: mueve la remesa (y en cascada todas sus facturas) a otra empresa de transporte."""

    empresa_id: int = Field(ge=1, description="Empresa de transporte de destino")
    conductor_id: int | None = Field(
        default=None, ge=1,
        description="Conductor de la empresa de destino. Vacío = la remesa queda sin conductor hasta que se reasigne",
    )


# ---------------------------------------------------------------- Lectura
class EventoOut(ORM):
    id: str
    estatus: str
    estatus_anterior: str | None
    ocurrido_at: datetime
    recibido_at: datetime
    origen: str
    motivo: str | None
    foto_id: str | None
    aplicado: bool
    usuario_nombre: str | None = None
    latitud: float | None = None
    longitud: float | None = None
    precision_m: float | None = None
    fotos: list[FotoEventoOut] = Field(default_factory=list, description="Todas las fotos de este estatus")


class FacturaConductorOut(ORM):
    id: int
    numero: str
    pedido: str | None
    cliente: str | None
    empresa: str | None
    bultos: int | None
    comentario: str | None
    observacion_entrega: str | None = None
    estatus: str
    estatus_at: datetime
    tracking: str | None
    remesa_id: int
    remesa_numero: str
    destino: str | None
    tiene_foto: bool = False


class EventoConductorOut(ORM):
    id: str
    estatus: str
    ocurrido_at: datetime
    aplicado: bool
    motivo: str | None
    foto_id: str | None


class FacturaConductorDetalleOut(FacturaConductorOut):
    eventos: list[EventoConductorOut] = Field(default_factory=list)


class RemesaConductorOut(BaseModel):
    """Vista de remesa para el conductor: sin montos ni datos de otros conductores."""

    id: int
    numero: str
    empresa: str | None
    destino: str | None
    placa: str | None
    created_at: datetime
    total_facturas: int
    por_estatus: dict[str, int]
    progreso: float


class RemesaConductorDetalleOut(RemesaConductorOut):
    facturas: list[FacturaConductorOut] = Field(default_factory=list)


class FacturaOut(ORM):
    id: int
    numero: str
    pedido: str | None
    cod_vendedor: str | None
    cliente: str | None
    empresa: str | None
    fecha_fac: date | None
    fecha_alm: datetime | None = Field(description="Fecha Preparado")
    fecha_rec: datetime | None = Field(description="Fecha Entregado (vacía = Por Entregar)")
    fecha_tra: datetime | None = Field(default=None, description="Fecha Por Entregar")
    bultos: int | None
    # Montos: solo para SUPERVISOR y COBRANZAS; para el resto la API los devuelve vacíos
    total_fact: Decimal | None
    porcentaje: Decimal | None
    total_neto: Decimal | None
    flete_pct: Decimal | None
    total_flete: Decimal | None
    montos_visibles: bool = True
    comentario: str | None
    estatus: str
    estatus_at: datetime
    tracking: str | None
    remesa_id: int
    empresa_id: int | None = None
    evidencia_foto_id: str | None = None
    evidencia_at: datetime | None = None
    observacion_entrega: str | None = None
    reingreso_de_id: int | None = None
    remesa_numero: str | None = None
    conductor_nombre: str | None = None
    # Nombre de la empresa de transporte. El alias de lectura evita tomar la relación ORM ``Remesa.empresa_transporte``
    # (el endpoint lo rellena); en la respuesta se llama ``empresa_transporte``.
    empresa_transporte: str | None = Field(
        default=None, validation_alias="empresa_transporte_nombre", serialization_alias="empresa_transporte",
        description="Nombre de la empresa de transporte",
    )


class CicloFacturaOut(BaseModel):
    """Otro ciclo de envío de la misma factura (antes o después de una devolución)."""

    id: int
    remesa_numero: str | None
    estatus: str
    estatus_at: datetime


class TramoProcesoOut(BaseModel):
    """Tiempo de jornada laboral (lunes a viernes, 7:00–16:00 por defecto) entre dos hitos de la factura."""

    desde: str  # nombre del hito inicial (p. ej. «Fecha Factura»)
    hasta: str
    inicio: datetime | None
    fin: datetime | None
    minutos: int | None = Field(description="Minutos hábiles del tramo")
    texto: str | None = Field(description="«2 días 3 h 05 min» o «3 h 05 min»")
    estado: Literal["completo", "en_curso", "pendiente", "sin_dato"]


class TiemposProcesoOut(BaseModel):
    """DÍAS TRANSCURRIDOS ENTRE PROCESOS: Factura → Preparado → Por Entregar → Entregado (o Devolución)."""

    tramos: list[TramoProcesoOut]
    total_minutos: int | None = Field(description="Total para la entrega (suma de los tramos, minutos hábiles)")
    total_texto: str | None
    entregada: bool = Field(description="True cuando los tres tramos están completos")
    en_curso: bool = Field(description="El total cuenta hasta ahora porque la entrega no ha terminado")
    devolucion: bool = Field(default=False, description="Solo se mide Por Entregar → Devolución")
    mensaje: str | None = None
    aviso: str | None = None
    nota: str | None = Field(default=None, description="Jornada usada: «Días hábiles de 9 h: lunes a viernes…»")


class FacturaDetalleOut(FacturaOut):
    eventos: list[EventoOut] = Field(default_factory=list)
    tiempos: TiemposProcesoOut | None = None
    captura_id: str | None = Field(default=None, description="Foto original de la factura, si se dio de alta por foto")
    tiene_foto: bool = Field(default=False, description="La factura ya tiene alguna foto de entrega o evidencia")
    fotos_sueltas: list[str] = Field(
        default_factory=list, description="Fotos de la factura que no están ligadas a un evento del historial")
    adjuntar_foto: bool = Field(
        default=False, description="El estatus actual (Por Entregar, No Entregado o Entregada) aún no tiene foto")
    habilitada_reingreso: bool = Field(default=False, description="Devuelta y aún sin reingresar en otra remesa")
    reingreso_de: CicloFacturaOut | None = None
    reingresos: list[CicloFacturaOut] = Field(default_factory=list)


class RemesaOut(ORM):
    id: int
    numero: str
    numero_generado: bool
    empresa: str | None
    empresa_id: int | None = None
    destino: str | None
    conductor_id: int | None
    chofer_nombre: str | None
    chofer_cedula: str | None = None
    chofer_telefono: str | None = None
    placa: str | None
    created_at: datetime
    total_facturas: int = 0
    por_estatus: dict[str, int] = Field(default_factory=dict)
    progreso: float = 0.0
    # Nombre de la empresa de transporte. El alias de lectura evita tomar la relación ORM ``Remesa.empresa_transporte``
    # (el endpoint lo rellena); en la respuesta se llama ``empresa_transporte``.
    empresa_transporte: str | None = Field(
        default=None, validation_alias="empresa_transporte_nombre", serialization_alias="empresa_transporte",
        description="Nombre de la empresa de transporte",
    )


class RemesaDetalleOut(RemesaOut):
    facturas: list[FacturaOut] = Field(default_factory=list)


class CambioEmpresaTransporteOut(BaseModel):
    remesa: RemesaOut
    facturas_actualizadas: int
    capturas_actualizadas: int
    empresa_anterior: str | None
    empresa_nueva: str
    conductor_nombre: str | None = Field(description="Conductor asignado; vacío = la remesa quedó sin conductor")


class Pagina(BaseModel):
    total: int
    items: list[Any]


# ---------------------------------------------------------- Consulta pública
class FotoPublicaOut(BaseModel):
    url: str
    expira_at: datetime
    capturada_at: datetime | None = None


class HitoPublico(BaseModel):
    estatus: str
    fecha: datetime
    con_evidencia: bool = False
    # Proof of Delivery del hito de cierre vigente (Entregada / Devolución): URL pre-firmada y temporal.
    # Equivale a un `delivery_proof_url`; solo el hito cuya foto respalda el estatus actual la trae.
    evidencia_url: str | None = None
    evidencia_expira_at: datetime | None = None
    observacion: str | None = Field(default=None, description="Observación que dejó el conductor al entregar")
    fotos: list[FotoPublicaOut] = Field(default_factory=list, description="Fotos de este estatus (enlaces temporales)")


class EvidenciaPublicaOut(BaseModel):
    """Proof of Delivery expuesto al vendedor/cliente: enlace temporal firmado, nunca el id interno."""

    estatus: str = Field(description="Estatus de cierre que respalda la foto (Entregada / Devolución)")
    url: str = Field(description="URL pre-firmada (HMAC) de solo lectura; caduca en `expira_at`")
    expira_at: datetime
    content_type: str
    capturada_at: datetime


class TrackingPublicoOut(BaseModel):
    numero: str
    pedido: str | None
    tracking: str | None
    empresa: str | None
    cliente: str | None
    destino: str | None
    estatus: str
    estatus_at: datetime
    historial: list[HitoPublico]
    # Solo cuando el estatus vigente es de cierre y hay foto: respaldo visual de la entrega/devolución
    evidencia: EvidenciaPublicaOut | None = None
    # Días y horas hábiles transcurridos entre procesos (mismo cálculo que el panel interno)
    tiempos: TiemposProcesoOut | None = None


class ConsultaPublicaOut(BaseModel):
    encontrado: bool
    mensaje: str | None = None
    resultados: list[TrackingPublicoOut] = Field(default_factory=list)


# ----------------------------------------------------------- Notificaciones
class NotificarVendedoresIn(BaseModel):
    modo: Literal["en_curso", "final"]
    desde: date | None = None
    hasta: date | None = None
    remesa_ids: list[int] | None = None
    simular: bool = Field(default=False, description="Arma los correos pero no los envía")


class NotificarCobranzasIn(BaseModel):
    desde: date | None = None
    hasta: date | None = None
    remesa_ids: list[int] | None = None
    solo_finales: bool = False
    simular: bool = False


class ColumnaReporteOut(BaseModel):
    clave: str
    titulo: str
    tipo: Literal["texto", "fecha", "fechahora", "entero", "monto", "pct"]


class TablaCobranzasOut(BaseModel):
    total: int
    offset: int
    limit: int
    orden: str | None
    desc: bool
    columnas: list[ColumnaReporteOut]
    items: list[dict[str, Any]]
    totales: dict[str, Any]


class RolOut(BaseModel):
    valor: Rol
