"""Enumeraciones del dominio."""

from __future__ import annotations

from enum import Enum


class Rol(str, Enum):
    SUPERVISOR = "supervisor"
    TRANSPORTISTA = "transportista"
    CONDUCTOR = "conductor"
    COBRANZAS = "cobranzas"
    CONSULTOR = "consultor"  # ve todo el sistema (todas las empresas) en modo solo consulta


class Estatus(str, Enum):
    """Flujo: Preparado → Por Entregar → (No Entregado) → Entregada | Devolución.

    Los valores internos (``recibida``, ``en_transito``) no cambian para no romper los enlaces, los
    reportes ni los cambios que los teléfonos tengan guardados sin señal: solo cambia el nombre visible.
    El antiguo estatus «Cargado» se eliminó (migración 0010).
    «No Entregado» (cliente no disponible) no es final: la factura sigue pendiente de entrega o devolución.
    """

    RECIBIDA = "recibida"  # se muestra como «Preparado»
    EN_TRANSITO = "en_transito"  # se muestra como «Por Entregar»
    NO_ENTREGADO = "no_entregado"  # «No se entregó - Cliente no Disponible»
    ENTREGADA = "entregada"
    DEVOLUCION = "devolucion"

    @property
    def label(self) -> str:
        return ESTATUS_LABELS[self]

    @property
    def orden(self) -> int:
        return ESTATUS_ORDEN[self]

    @property
    def es_final(self) -> bool:
        return self in (Estatus.ENTREGADA, Estatus.DEVOLUCION)


ESTATUS_LABELS: dict[Estatus, str] = {
    Estatus.RECIBIDA: "Preparado",
    Estatus.EN_TRANSITO: "Por Entregar",
    Estatus.NO_ENTREGADO: "No Entregado",
    Estatus.ENTREGADA: "Entregada",
    Estatus.DEVOLUCION: "Devolución",
}
MENSAJE_NO_ENTREGADO = "No se entregó - Cliente no Disponible"

ESTATUS_ORDEN: dict[Estatus, int] = {
    Estatus.RECIBIDA: 0,
    Estatus.EN_TRANSITO: 1,
    Estatus.NO_ENTREGADO: 2,
    Estatus.ENTREGADA: 3,
    Estatus.DEVOLUCION: 3,
}

# Estatus que ya no existen y a qué estatus vigente equivalen. Un teléfono con la versión anterior
# puede tener guardado sin señal un «Cargado»: al sincronizar se registra como «Por Entregar».
ESTATUS_RETIRADOS: dict[str, Estatus] = {"cargado": Estatus.EN_TRANSITO}


class TipoEventoOperativo(str, Enum):
    """Eventos del ciclo de transporte que el conductor puede registrar (y nada más)."""

    SALIDA = "salida"  # «Por Entregar»
    NO_ENTREGADO = "no_entregado"  # cliente no disponible
    ENTREGA = "entrega"
    DEVOLUCION = "devolucion"

    @property
    def estatus(self) -> Estatus:
        return EVENTO_A_ESTATUS[self]


EVENTO_A_ESTATUS: dict[TipoEventoOperativo, Estatus] = {
    TipoEventoOperativo.SALIDA: Estatus.EN_TRANSITO,
    TipoEventoOperativo.NO_ENTREGADO: Estatus.NO_ENTREGADO,
    TipoEventoOperativo.ENTREGA: Estatus.ENTREGADA,
    TipoEventoOperativo.DEVOLUCION: Estatus.DEVOLUCION,
}
# Únicos valores de estatus que un CONDUCTOR puede asignar
ESTATUS_OPERATIVOS: frozenset[Estatus] = frozenset(EVENTO_A_ESTATUS.values())


class AccionAuditoria(str, Enum):
    USUARIO_CREADO = "usuario_creado"
    CONDUCTOR_CREADO = "conductor_creado"
    USUARIO_EDITADO = "usuario_editado"
    USUARIO_ELIMINADO = "usuario_eliminado"
    CLAVE_CAMBIADA = "clave_cambiada"  # el propio usuario
    CLAVE_ESTABLECIDA = "clave_establecida"  # un administrador fijó una clave concreta
    CLAVE_RESTABLECIDA = "clave_restablecida"  # un administrador generó una clave temporal
    CREDENCIAL_CONSULTADA = "credencial_consultada"
    EMPRESA_CREADA = "empresa_creada"
    EMPRESA_EDITADA = "empresa_editada"
    EMPRESA_ELIMINADA = "empresa_eliminada"
    EMPRESA_DESACTIVADA = "empresa_desactivada"
    USUARIO_ACTIVADO = "usuario_activado"
    USUARIO_DESACTIVADO = "usuario_desactivado"
    BITACORA_ARCHIVADA = "bitacora_archivada"
    BASE_INICIALIZADA = "base_inicializada"  # limpieza con tracking.inicializar_bd
    EMPRESA_TRANSPORTE_CAMBIADA = "empresa_transporte_cambiada"  # remesa y sus facturas a otra empresa
    BIOMETRIA_ACTIVADA = "biometria_activada"  # el usuario registró la huella/rostro de un teléfono
    BIOMETRIA_ELIMINADA = "biometria_eliminada"  # la quitó él mismo o se revocó al restablecer su clave


class EstadoImportacion(str, Enum):
    PREVIEW = "preview"
    CONFIRMADA = "confirmada"
    DESCARTADA = "descartada"


class OrigenEvento(str, Enum):
    IMPORTACION = "importacion"
    ONLINE = "online"
    OFFLINE = "offline"
    ADMIN = "admin"
    CAPTURA = "captura"  # alta desde la foto de una factura en papel (OCR + validación humana)


class EstadoCaptura(str, Enum):
    PENDIENTE = "pendiente"  # extraída, esperando la revisión del operador
    CONFIRMADA = "confirmada"  # el operador validó los datos: ya existe la factura
    DESCARTADA = "descartada"


class AccionBitacora(str, Enum):
    """Tipo de actividad registrada en la bitácora."""

    LOGIN = "LOGIN"
    LOGIN_FALLIDO = "LOGIN_FALLIDO"
    LOGOUT = "LOGOUT"
    VISTA = "VISTA"  # abrió una pantalla/módulo
    CONSULTAR = "CONSULTAR"  # READ
    CREAR = "CREAR"  # CREATE
    MODIFICAR = "MODIFICAR"  # UPDATE
    ELIMINAR = "ELIMINAR"  # DELETE
