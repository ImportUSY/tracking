"""Modelo de datos (SQLAlchemy 2.0, tipado)."""

from __future__ import annotations

import builtins
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tracking.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator[datetime]):
    """Guarda siempre en UTC y devuelve datetimes con zona (SQLite no conserva tz)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


MONEY = Numeric(16, 4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, onupdate=utcnow, nullable=False
    )


class EmpresaTransporte(TimestampMixin, Base):
    """Empresa de transporte (inquilino lógico). Agrupa transportistas, conductores, remesas y facturas.

    No confundir con ``Remesa.empresa`` / ``Factura.empresa``: esas columnas de texto son la empresa
    *emisora* de la factura tal como viene en el Excel, no la empresa que la transporta.
    """

    __tablename__ = "empresas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(160), unique=True)
    rif: Mapped[str | None] = mapped_column(String(20), unique=True)
    telefono: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(200))
    direccion: Mapped[str | None] = mapped_column(Text)
    activo: Mapped[bool] = mapped_column(Boolean, default=True)


class PerteneceAEmpresa:
    """Mixin de las entidades aisladas por inquilino (Remesa, Factura, Importacion).

    ``tracking.tenancy`` aplica sobre esta clase el filtro global ``empresa_id = :empresa`` a toda
    lectura ORM de un usuario TRANSPORTISTA o CONDUCTOR, y valida las escrituras antes del flush.
    ``NULL`` significa "sin empresa": solo visible para los roles globales (Supervisor, Cobranzas).
    """

    empresa_id: Mapped[int | None] = mapped_column(
        ForeignKey("empresas.id", ondelete="RESTRICT"), index=True, nullable=True
    )


class Usuario(TimestampMixin, Base):
    __tablename__ = "usuarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    nombre: Mapped[str] = mapped_column(String(160))
    rol: Mapped[str] = mapped_column(String(20), index=True)
    # Inquilino del usuario (obligatorio para TRANSPORTISTA y CONDUCTOR; opcional para roles globales)
    empresa_id: Mapped[int | None] = mapped_column(
        ForeignKey("empresas.id", ondelete="RESTRICT", name="fk_usuarios_empresa"), index=True
    )
    email: Mapped[str | None] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(255))
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    debe_cambiar_clave: Mapped[bool] = mapped_column(Boolean, default=False)
    # Ingresos hechos con la clave temporal (se reinicia al asignar una nueva temporal o al cambiarla)
    accesos_clave_temporal: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    ultimo_acceso_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # Trazabilidad de credenciales (la clave nunca se guarda en claro: solo su hash Argon2id)
    clave_actualizada_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    clave_actualizada_por_id: Mapped[int | None] = mapped_column(
        ForeignKey("usuarios.id", ondelete="SET NULL", name="fk_usuarios_clave_actualizada_por")
    )

    conductor: Mapped[Conductor | None] = relationship(back_populates="usuario", uselist=False)
    clave_actualizada_por: Mapped[Usuario | None] = relationship(remote_side=[id])
    empresa: Mapped[EmpresaTransporte | None] = relationship()


class CredencialBiometrica(Base):
    """Llave de acceso (WebAuthn) de un teléfono: permite entrar con la huella o el rostro.

    La huella nunca sale del teléfono: aquí solo se guarda la llave *pública* que el teléfono generó para
    este sitio. Se borra con el usuario (ON DELETE CASCADE) y al restablecerle la clave un administrador.
    """

    __tablename__ = "credenciales_biometricas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id", ondelete="CASCADE"), index=True)
    credencial_id: Mapped[str] = mapped_column(String(512), unique=True)  # base64url
    llave_publica: Mapped[builtins.bytes] = mapped_column(LargeBinary)  # COSE
    contador: Mapped[int] = mapped_column(Integer, default=0)
    rp_id: Mapped[str] = mapped_column(String(253))
    dispositivo: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    ultimo_uso_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class RetoBiometrico(Base):
    """Reto de un solo uso para registrar o usar una llave de acceso (vence en pocos minutos).

    Vive en la base y no en memoria porque en Vercel cada petición puede atenderla una instancia distinta.
    """

    __tablename__ = "retos_biometricos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    reto: Mapped[builtins.bytes] = mapped_column(LargeBinary)
    proposito: Mapped[str] = mapped_column(String(10))  # "registro" | "ingreso"
    usuario_id: Mapped[int | None] = mapped_column(Integer)
    expira_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


class Conductor(TimestampMixin, Base):
    __tablename__ = "conductores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cedula: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    nombre: Mapped[str] = mapped_column(String(160))
    telefono: Mapped[str | None] = mapped_column(String(40))
    placa: Mapped[str | None] = mapped_column(String(20))
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    usuario_id: Mapped[int | None] = mapped_column(
        ForeignKey("usuarios.id", ondelete="SET NULL"), unique=True
    )

    usuario: Mapped[Usuario | None] = relationship(back_populates="conductor")


class Camion(TimestampMixin, Base):
    __tablename__ = "camiones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    placa: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    color: Mapped[str | None] = mapped_column(String(40))
    serial: Mapped[str | None] = mapped_column(String(60))
    clase: Mapped[str | None] = mapped_column(String(40))
    activo: Mapped[bool] = mapped_column(Boolean, default=True)


class Vendedor(TimestampMixin, Base):
    __tablename__ = "vendedores"

    codigo: Mapped[str] = mapped_column(String(20), primary_key=True)
    nombre: Mapped[str] = mapped_column(String(160))
    territorio: Mapped[str | None] = mapped_column(String(80))
    email: Mapped[str | None] = mapped_column(String(200))
    activo: Mapped[bool] = mapped_column(Boolean, default=True)


class Importacion(PerteneceAEmpresa, Base):
    __tablename__ = "importaciones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    archivo_nombre: Mapped[str] = mapped_column(String(255))
    archivo_sha256: Mapped[str] = mapped_column(String(64), index=True)
    hoja: Mapped[str] = mapped_column(String(120))
    estado: Mapped[str] = mapped_column(String(20), index=True)
    creado_por_id: Mapped[int | None] = mapped_column(ForeignKey("usuarios.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    confirmado_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    resumen: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    datos: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    resultado: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    revision_ia: Mapped[str | None] = mapped_column(Text)


class Remesa(PerteneceAEmpresa, TimestampMixin, Base):
    __tablename__ = "remesas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    numero: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    numero_generado: Mapped[bool] = mapped_column(Boolean, default=False)
    empresa: Mapped[str | None] = mapped_column(String(120))
    destino: Mapped[str | None] = mapped_column(String(160))
    conductor_id: Mapped[int | None] = mapped_column(
        ForeignKey("conductores.id", ondelete="SET NULL"), index=True
    )
    chofer_cedula: Mapped[str | None] = mapped_column(String(20))
    chofer_nombre: Mapped[str | None] = mapped_column(String(160))
    chofer_telefono: Mapped[str | None] = mapped_column(String(40))
    placa: Mapped[str | None] = mapped_column(String(20))
    importacion_id: Mapped[int | None] = mapped_column(ForeignKey("importaciones.id", ondelete="SET NULL"))
    fila_origen: Mapped[int | None] = mapped_column(Integer)

    conductor: Mapped[Conductor | None] = relationship()
    empresa_transporte: Mapped[EmpresaTransporte | None] = relationship()
    facturas: Mapped[list[Factura]] = relationship(
        back_populates="remesa", cascade="all, delete-orphan", order_by="Factura.id"
    )


class Factura(PerteneceAEmpresa, TimestampMixin, Base):
    """Factura transportada. ``empresa_id`` se desnormaliza desde la remesa para filtrar sin JOIN."""

    __tablename__ = "facturas"
    __table_args__ = (
        UniqueConstraint("remesa_id", "numero", name="uq_factura_remesa_numero"),
        Index("ix_facturas_conductor_estatus", "conductor_id", "estatus"),
        Index("ix_facturas_empresa_estatus", "empresa_id", "estatus"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    remesa_id: Mapped[int] = mapped_column(ForeignKey("remesas.id", ondelete="CASCADE"), index=True)
    conductor_id: Mapped[int | None] = mapped_column(ForeignKey("conductores.id", ondelete="SET NULL"))
    empresa: Mapped[str | None] = mapped_column(String(120))
    numero: Mapped[str] = mapped_column(String(40), index=True)
    pedido: Mapped[str | None] = mapped_column(String(40), index=True)
    cod_vendedor: Mapped[str | None] = mapped_column(String(20), index=True)
    cliente: Mapped[str | None] = mapped_column(String(255))
    fecha_fac: Mapped[date | None] = mapped_column(Date)
    # Fecha y hora (UTC; se muestran en hora local). Del Excel solo llega el día: queda a las 00:00 locales.
    fecha_alm: Mapped[datetime | None] = mapped_column(UTCDateTime())  # Fecha Preparado (del Excel o de la foto)
    fecha_rec: Mapped[datetime | None] = mapped_column(UTCDateTime())  # Fecha Entregado (al pasar a Entregada)
    fecha_tra: Mapped[datetime | None] = mapped_column(UTCDateTime())  # Fecha Por Entregar (estatus Por Entregar)
    bultos: Mapped[int | None] = mapped_column(Integer)
    total_fact: Mapped[Decimal | None] = mapped_column(MONEY)
    porcentaje: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    total_neto: Mapped[Decimal | None] = mapped_column(MONEY)
    flete_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    total_flete: Mapped[Decimal | None] = mapped_column(MONEY)
    comentario: Mapped[str | None] = mapped_column(Text)
    fila_origen: Mapped[int | None] = mapped_column(Integer)

    estatus: Mapped[str] = mapped_column(String(20), index=True)
    estatus_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    tracking: Mapped[str | None] = mapped_column(String(20), unique=True, index=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
    # Proof of Delivery: foto que respalda el estatus de cierre vigente (Entregada / Devolución).
    # Puntero desnormalizado del último evento aplicado con foto: la consulta pública no recorre el historial.
    evidencia_foto_id: Mapped[str | None] = mapped_column(
        ForeignKey("fotos.id", ondelete="SET NULL", name="fk_facturas_evidencia_foto", use_alter=True)
    )
    evidencia_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # Observación que dejó el conductor al entregar (vigente mientras la factura esté Entregada; el
    # historial completo queda en eventos_estatus.motivo)
    observacion_entrega: Mapped[str | None] = mapped_column(Text)
    # Reingreso: esta factura es un nuevo ciclo de envío de una factura que fue devuelta antes
    reingreso_de_id: Mapped[int | None] = mapped_column(
        ForeignKey("facturas.id", ondelete="SET NULL", name="fk_facturas_reingreso_de"), index=True
    )

    remesa: Mapped[Remesa] = relationship(back_populates="facturas")
    eventos: Mapped[list[EventoEstatus]] = relationship(
        back_populates="factura", cascade="all, delete-orphan", order_by="EventoEstatus.ocurrido_at"
    )
    evidencia: Mapped[Foto | None] = relationship(foreign_keys=[evidencia_foto_id], post_update=True)


class Foto(Base):
    __tablename__ = "fotos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    factura_id: Mapped[int] = mapped_column(ForeignKey("facturas.id", ondelete="CASCADE"), index=True)
    ruta: Mapped[str] = mapped_column(String(500))
    content_type: Mapped[str] = mapped_column(String(60))
    bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    subido_por_id: Mapped[int | None] = mapped_column(ForeignKey("usuarios.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    # Metadatos de evidencia (PoD): momento de la captura en el dispositivo y, si el binario vive en un
    # object storage externo (S3/GCS/R2), su URL o clave canónica (nunca una URL pre-firmada: caducan).
    capturada_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # Evento del historial al que pertenece la foto (un estatus puede tener varias fotos). Sin clave
    # foránea: la foto se sube antes que el evento (sin señal, el evento llega después).
    evento_id: Mapped[str | None] = mapped_column(String(36), index=True)
    url_externa: Mapped[str | None] = mapped_column(String(1000))
    # Contenido de la imagen cuando STORAGE_BACKEND=db (Vercel no tiene disco persistente).
    datos: Mapped[Optional[builtins.bytes]] = mapped_column(LargeBinary, nullable=True, deferred=True)  # noqa: UP045


class EventoEstatus(Base):
    __tablename__ = "eventos_estatus"

    # UUID generado por el cliente: clave de idempotencia para la cola offline.
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    factura_id: Mapped[int] = mapped_column(ForeignKey("facturas.id", ondelete="CASCADE"), index=True)
    estatus: Mapped[str] = mapped_column(String(20))
    estatus_anterior: Mapped[str | None] = mapped_column(String(20))
    ocurrido_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    recibido_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    usuario_id: Mapped[int | None] = mapped_column(ForeignKey("usuarios.id", ondelete="SET NULL"))
    origen: Mapped[str] = mapped_column(String(20))
    motivo: Mapped[str | None] = mapped_column(Text)
    foto_id: Mapped[str | None] = mapped_column(ForeignKey("fotos.id", ondelete="SET NULL"))
    # False cuando llegó tarde (offline) y ya existía un cambio posterior: queda en el historial.
    aplicado: Mapped[bool] = mapped_column(Boolean, default=True)
    # Ubicación GPS del teléfono al registrar el evento (la envía la app del conductor si pudo obtenerla)
    latitud: Mapped[float | None] = mapped_column(Float)
    longitud: Mapped[float | None] = mapped_column(Float)
    precision_m: Mapped[float | None] = mapped_column(Float)

    factura: Mapped[Factura] = relationship(back_populates="eventos")
    usuario: Mapped[Usuario | None] = relationship()
    foto: Mapped[Foto | None] = relationship()


class CapturaFactura(PerteneceAEmpresa, Base):
    """Foto de una factura en papel + datos extraídos por OCR/visión + datos validados por el operador.

    Flujo human-in-the-loop: ``pendiente`` (lo que leyó la IA, en ``datos_extraidos``) →
    ``confirmada`` (lo que el operador aprobó, en ``datos_validados``, y la factura creada) o ``descartada``.
    La imagen se conserva como respaldo (disco, base o ``url_externa`` si se migra a object storage).
    """

    __tablename__ = "capturas_factura"
    __table_args__ = (Index("ix_capturas_empresa_estado", "empresa_id", "estado"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    estado: Mapped[str] = mapped_column(String(20), index=True)
    # Imagen
    archivo_nombre: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(60))
    bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    ruta: Mapped[str] = mapped_column(String(500))  # ruta relativa en disco o "db"
    url_externa: Mapped[str | None] = mapped_column(String(1000))
    datos: Mapped[Optional[builtins.bytes]] = mapped_column(LargeBinary, nullable=True, deferred=True)  # noqa: UP045
    # Extracción (IA) y validación (humano)
    motor: Mapped[str] = mapped_column(String(80))  # p. ej. "anthropic:claude-sonnet-4-5" o "manual"
    datos_extraidos: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    avisos: Mapped[list[str]] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    datos_validados: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # Resultado
    factura_id: Mapped[int | None] = mapped_column(ForeignKey("facturas.id", ondelete="SET NULL"), index=True)
    remesa_id: Mapped[int | None] = mapped_column(ForeignKey("remesas.id", ondelete="SET NULL"))
    creado_por_id: Mapped[int | None] = mapped_column(ForeignKey("usuarios.id", ondelete="SET NULL"))
    confirmado_por_id: Mapped[int | None] = mapped_column(ForeignKey("usuarios.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    confirmado_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # Hora en que el teléfono tomó la foto (Fecha Preparado de la factura); si no la informa, created_at
    tomada_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ConsultaPublica(Base):
    """Registro anónimo de consultas públicas (métrica 'vendedores autónomos')."""

    __tablename__ = "consultas_publicas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    termino: Mapped[str] = mapped_column(String(60))
    encontrado: Mapped[bool] = mapped_column(Boolean)
    ip_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class Auditoria(Base):
    """Bitácora de acciones sensibles (altas, cambios de rol, claves). Nunca guarda claves ni hashes."""

    __tablename__ = "auditoria"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("usuarios.id", ondelete="SET NULL"))
    actor_rol: Mapped[str | None] = mapped_column(String(20))
    accion: Mapped[str] = mapped_column(String(40), index=True)
    objetivo_usuario_id: Mapped[int | None] = mapped_column(
        ForeignKey("usuarios.id", ondelete="SET NULL"), index=True
    )
    detalle: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Bitacora(Base):
    """Bitácora de actividad: cada acción de cada usuario (inicio/cierre de sesión, vistas, consultas,
    altas, cambios y bajas). **Inmutable**: no hay endpoints de edición y la base de datos rechaza
    UPDATE y DELETE con un trigger (migración 0008). Sin claves foráneas a propósito: el registro es
    una fotografía (nombre, rol y empresa del momento) que sobrevive aunque el usuario se elimine.
    """

    __tablename__ = "bitacora"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ocurrido_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    usuario_id: Mapped[int | None] = mapped_column(Integer, index=True)
    usuario: Mapped[str | None] = mapped_column(String(64))  # nombre de acceso
    usuario_nombre: Mapped[str | None] = mapped_column(String(160))
    rol: Mapped[str | None] = mapped_column(String(20), index=True)
    empresa_id: Mapped[int | None] = mapped_column(Integer, index=True)
    empresa_nombre: Mapped[str | None] = mapped_column(String(160))
    accion: Mapped[str] = mapped_column(String(20), index=True)  # LOGIN, LOGOUT, VISTA, CONSULTAR, CREAR…
    modulo: Mapped[str | None] = mapped_column(String(40), index=True)
    metodo: Mapped[str | None] = mapped_column(String(8))
    ruta: Mapped[str] = mapped_column(String(300))
    recurso_id: Mapped[str | None] = mapped_column(String(64))
    estado_http: Mapped[int | None] = mapped_column(Integer)
    exito: Mapped[bool] = mapped_column(Boolean, default=True)
    detalle: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    request_id: Mapped[str | None] = mapped_column(String(32))
    duracion_ms: Mapped[int | None] = mapped_column(Integer)


class BitacoraArchivo(Base):
    """Un período cerrado de la bitácora (mes o año) sacado de la tabla activa y guardado comprimido.

    El contenido son las filas originales en JSON Lines + gzip, con su SHA-256 para verificar que no se
    alteró. También es inmutable (trigger en la migración 0009) y se descarga como CSV o tal cual.
    """

    __tablename__ = "bitacora_archivos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tipo: Mapped[str] = mapped_column(String(4))  # "mes" | "anio"
    periodo: Mapped[str] = mapped_column(String(7), unique=True)  # "2026-09" | "2026"
    desde: Mapped[datetime] = mapped_column(UTCDateTime())  # inicio del período (00:00 local), incluido
    hasta: Mapped[datetime] = mapped_column(UTCDateTime())  # fin del período, excluido
    filas: Mapped[int] = mapped_column(Integer)
    primer_id: Mapped[int | None] = mapped_column(Integer)
    ultimo_id: Mapped[int | None] = mapped_column(Integer)
    bytes: Mapped[int] = mapped_column(Integer)  # tamaño comprimido
    sha256: Mapped[str] = mapped_column(String(64))
    formato: Mapped[str] = mapped_column(String(20), default="jsonl.gz")
    contenido: Mapped[builtins.bytes] = mapped_column(LargeBinary, deferred=True)
    creado_por_id: Mapped[int | None] = mapped_column(Integer)
    creado_por: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class Feriado(Base):
    """Día feriado: no cuenta como día hábil en los tiempos entre procesos (aunque caiga de lunes a viernes)."""

    __tablename__ = "feriados"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fecha: Mapped[date] = mapped_column(Date, unique=True, index=True)
    nombre: Mapped[str] = mapped_column(String(120))
    nacional: Mapped[bool] = mapped_column(Boolean, default=False)  # cargado de la lista nacional automática
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)


class ConsumoDatos(Base):
    """Datos móviles (red celular) que usó la app del conductor: bytes enviados y recibidos por conexión,
    método + ruta, día y hora local. Lo mide el teléfono; el tráfico por Wi-Fi no se registra nunca.

    Una fila acumula (el teléfono envía sumas parciales y el servidor las suma a la fila de la misma clave);
    ``desde`` / ``hasta`` son la fecha y hora exactas de la primera y la última petición de esa fila.
    Sin claves foráneas, como la bitácora: nombre y empresa son los del momento y sobreviven al usuario.
    """

    __tablename__ = "consumo_datos"
    __table_args__ = (
        UniqueConstraint("usuario_id", "conexion_id", "metodo", "ruta", "dia", "hora", name="uq_consumo_datos_hora"),
        Index("ix_consumo_datos_dia_usuario", "dia", "usuario_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dia: Mapped[date] = mapped_column(Date)
    hora: Mapped[int] = mapped_column(SmallInteger, default=0)  # 0-23, hora local
    usuario_id: Mapped[int] = mapped_column(Integer)
    usuario: Mapped[str | None] = mapped_column(String(64))
    usuario_nombre: Mapped[str | None] = mapped_column(String(160))
    empresa_id: Mapped[int | None] = mapped_column(Integer, index=True)
    empresa_nombre: Mapped[str | None] = mapped_column(String(160))
    conexion_id: Mapped[str] = mapped_column(String(36))
    tipo_red: Mapped[str] = mapped_column(String(12))  # siempre «cellular»
    tipo_efectivo: Mapped[str | None] = mapped_column(String(8))  # 4g, 3g, 2g, slow-2g
    metodo: Mapped[str] = mapped_column(String(8))
    ruta: Mapped[str] = mapped_column(String(200))
    solicitudes: Mapped[int] = mapped_column(Integer, default=0)
    bytes_subida: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes_bajada: Mapped[int] = mapped_column(BigInteger, default=0)
    desde: Mapped[datetime] = mapped_column(UTCDateTime())
    hasta: Mapped[datetime] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
