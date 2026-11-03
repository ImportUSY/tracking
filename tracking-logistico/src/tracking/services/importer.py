"""Carga de la hoja REMESA en dos pasos: vista previa (resumen) y confirmación.

* La vista previa nunca escribe remesas ni facturas: solo guarda el resultado del análisis en
  `importaciones` para que la confirmación use exactamente lo que el Transportista revisó.
* La confirmación es idempotente: remesas por número y facturas por (remesa, número) tienen
  restricciones únicas; volver a cargar el mismo archivo no duplica nada.
* Multi-tenant: un Transportista solo carga en su empresa. Choferes, remesas o facturas que ya
  pertenecen a otra empresa se excluyen sin revelar sus datos (número de remesa, estatus, etc.).
  Cada remesa nueva nace en la empresa de su conductor (invariante remesa.empresa_id == conductor).
"""

from __future__ import annotations

import hashlib
import io
import logging
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracking.enums import EstadoImportacion, Estatus, OrigenEvento
from tracking.models import (
    Camion,
    Conductor,
    EventoEstatus,
    Factura,
    Importacion,
    Remesa,
    Usuario,
    Vendedor,
)
from tracking.services.ai import ClaudeClient
from tracking.services.reingresos import factura_devuelta_previa
from tracking.services.remesa_parser import (
    Aviso,
    BloqueRemesa,
    FiltroFechaFac,
    Nivel,
    RemesaParser,
    ResultadoParseo,
    clave_factura,
)
from tracking.services.timeutil import inicio_dia_local, now_utc
from tracking.services.users import crear_usuario_conductor
from tracking.tenancy import TODAS_LAS_EMPRESAS, TenantScope, scope_de_sesion

log = logging.getLogger(__name__)

CHUNK = 500
# Detalle por factura en la vista previa filtrada por Fecha Fac (tope para no exceder el tamaño de respuesta)
MAX_DETALLE_FACTURAS = 5000


class ImportacionError(Exception):
    def __init__(self, mensaje: str, status_code: int = 400) -> None:
        super().__init__(mensaje)
        self.mensaje = mensaje
        self.status_code = status_code


def _chunks(items: list[str], size: int = CHUNK) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


# ---------------------------------------------------------------- Clasificación
@dataclass
class EstadoBD:
    conductores: dict[str, Conductor]
    remesas: dict[str, Remesa]
    # clave_factura -> lista de (remesa_numero, estatus, es_de_otra_empresa)
    facturas: dict[str, list[tuple[str, str, bool]]]
    vendedores: set[str]
    camiones: set[str]
    # Multi-tenant: existen, pero en otra empresa (no se usan ni se muestran sus datos)
    conductores_ajenos: set[str] = field(default_factory=set)
    remesas_ajenas: set[str] = field(default_factory=set)


def _empresa_conductor(c: Conductor) -> int | None:
    return c.usuario.empresa_id if c.usuario is not None else None


def _cargar_estado_bd(db: Session, bloques: list[BloqueRemesa], scope: TenantScope | None = None) -> EstadoBD:
    """Estado actual de la base para clasificar. Las búsquedas por número/cédula son *globales*
    (TODAS_LAS_EMPRESAS) porque la unicidad lo es; luego se separa lo propio de lo ajeno."""
    scope = scope or scope_de_sesion(db)
    cedulas = sorted({b.chofer_cedula for b in bloques if b.chofer_cedula})
    numeros_rem = sorted({b.numero for b in bloques if b.numero})
    numeros_fac = sorted({f.numero for b in bloques for f in b.facturas})

    conductores: dict[str, Conductor] = {}
    conductores_ajenos: set[str] = set()
    for ch in _chunks(cedulas):
        for c in db.scalars(select(Conductor).where(Conductor.cedula.in_(ch))):
            if scope.permite(_empresa_conductor(c)):
                conductores[c.cedula] = c
            else:
                conductores_ajenos.add(c.cedula)

    remesas: dict[str, Remesa] = {}
    remesas_ajenas: set[str] = set()
    for ch in _chunks(numeros_rem):
        stmt_rem = select(Remesa.id, Remesa.numero, Remesa.empresa_id).where(Remesa.numero.in_(ch))
        visibles: list[int] = []
        for rid, numero, empresa_id in db.execute(stmt_rem.execution_options(**TODAS_LAS_EMPRESAS)):
            if scope.permite(empresa_id):
                visibles.append(rid)
            else:
                remesas_ajenas.add(numero)
        for r in db.scalars(select(Remesa).where(Remesa.id.in_(visibles))):
            remesas[r.numero] = r

    facturas: dict[str, list[tuple[str, str, bool]]] = defaultdict(list)
    for ch in _chunks(numeros_fac):
        stmt = (
            select(Factura.numero, Factura.empresa, Factura.estatus, Remesa.numero, Factura.empresa_id)
            .join(Remesa, Remesa.id == Factura.remesa_id)
            .where(Factura.numero.in_(ch))
            .order_by(Factura.id)
            .execution_options(**TODAS_LAS_EMPRESAS)
        )
        for numero, empresa, estatus, rem_num, empresa_id in db.execute(stmt):
            facturas[clave_factura(empresa, numero)].append((rem_num, estatus, not scope.permite(empresa_id)))

    vendedores = set(db.scalars(select(Vendedor.codigo)))
    camiones = set(db.scalars(select(Camion.placa)))
    return EstadoBD(conductores, remesas, dict(facturas), vendedores, camiones, conductores_ajenos, remesas_ajenas)


@dataclass
class Clasificacion:
    # numero_remesa -> lista de (numero_factura, clasificacion)
    facturas: dict[str, dict[str, str]] = field(default_factory=dict)
    avisos: list[Aviso] = field(default_factory=list)


def _nota_reingreso(previa: Factura | None) -> str | None:
    if previa is None:
        return None
    remesa = previa.remesa.numero if previa.remesa else "anterior"
    return f"Reingreso tras devolución (remesa {remesa})"


def _clasificar(bloques: list[BloqueRemesa], estado: EstadoBD) -> Clasificacion:
    """Para cada factura: nueva | ya_cargada | en_otra_remesa | en_otra_empresa | reenvio."""
    out = Clasificacion()
    vendedores_desconocidos: set[str] = set()
    placas_desconocidas: set[str] = set()
    for b in bloques:
        if b.excluida or not b.numero:
            continue
        clases: dict[str, str] = {}
        for f in b.facturas:
            previas = estado.facturas.get(clave_factura(b.empresa, f.numero), [])
            en_esta = [p for p in previas if p[0] == b.numero]
            otras = [p for p in previas if p[0] != b.numero]
            if en_esta:
                clases[f.numero] = "ya_cargada"
            elif otras and otras[-1][2] and otras[-1][1] != Estatus.DEVOLUCION.value:
                # Otra empresa la transporta: se omite sin revelar remesa ni estatus ajenos
                clases[f.numero] = "en_otra_empresa"
                out.avisos.append(Aviso("FACTURA_OTRA_EMPRESA", Nivel.ADVERTENCIA,
                                        f"Factura {f.numero} está asignada a otra empresa de transporte. Se omite.",
                                        fila=f.fila, remesa=b.numero, factura=f.numero))
            elif otras:
                ultima_rem, ultimo_estatus, ajena = otras[-1]
                ultima_rem = "de otra empresa" if ajena else ultima_rem
                if ultimo_estatus == Estatus.DEVOLUCION.value:
                    clases[f.numero] = "reenvio"
                    out.avisos.append(Aviso("FACTURA_REENVIO", Nivel.INFO,
                                            f"Factura {f.numero} fue devuelta en la remesa {ultima_rem}; "
                                            "se carga como reenvío.", fila=f.fila, remesa=b.numero,
                                            factura=f.numero))
                else:
                    clases[f.numero] = "en_otra_remesa"
                    out.avisos.append(Aviso("FACTURA_YA_EXISTE", Nivel.ADVERTENCIA,
                                            f"Factura {f.numero} ya está en el sistema (remesa {ultima_rem}, "
                                            f"estatus {Estatus(ultimo_estatus).label}). Se omite.",
                                            fila=f.fila, remesa=b.numero, factura=f.numero))
            else:
                clases[f.numero] = "nueva"
            if estado.vendedores and f.cod_vendedor and f.cod_vendedor not in estado.vendedores:
                vendedores_desconocidos.add(f.cod_vendedor)
        if estado.camiones and b.placa and b.placa not in estado.camiones:
            placas_desconocidas.add(b.placa)
        out.facturas[b.numero] = clases
    if vendedores_desconocidos:
        out.avisos.append(Aviso("VENDEDOR_DESCONOCIDO", Nivel.INFO,
                                "Códigos de vendedor que no están en el listado: "
                                + ", ".join(sorted(vendedores_desconocidos))))
    if placas_desconocidas:
        out.avisos.append(Aviso("PLACA_NO_REGISTRADA", Nivel.INFO,
                                "Placas que no están registradas como camión: "
                                + ", ".join(sorted(placas_desconocidas))))
    return out


def _resumen(
    parseo: ResultadoParseo,
    estado: EstadoBD,
    clas: Clasificacion,
    archivo: str,
    archivo_repetido: bool,
    mapeo_ia: dict[str, str],
) -> dict[str, Any]:
    remesas: list[dict[str, Any]] = []
    por_chofer: dict[str, dict[str, Any]] = {}
    avisos = [a.to_dict() for a in parseo.avisos + clas.avisos]
    # Con filtro por Fecha Fac: lista de las facturas del rango para validarlas antes de confirmar (sin montos)
    detalle: list[dict[str, Any]] | None = [] if parseo.filtro else None

    for b in parseo.bloques:
        registrado = bool(b.chofer_cedula and b.chofer_cedula in estado.conductores)
        chofer_ajeno = bool(b.chofer_cedula and b.chofer_cedula in estado.conductores_ajenos)
        clases = clas.facturas.get(b.numero or "", {})
        nuevas = sum(1 for c in clases.values() if c in ("nueva", "reenvio"))
        if detalle is not None:
            for f in b.facturas:
                if len(detalle) >= MAX_DETALLE_FACTURAS:
                    break
                detalle.append({
                    "remesa": b.numero, "factura": f.numero, "fila": f.fila,
                    "fecha_fac": f.fecha_fac.isoformat() if f.fecha_fac else None,
                    "cliente": f.cliente, "pedido": f.pedido, "cod_vendedor": f.cod_vendedor, "bultos": f.bultos,
                    "chofer": b.chofer_nombre, "destino": b.destino,
                    "clase": "remesa_excluida" if b.excluida else clases.get(f.numero),
                })
        if b.excluida:
            estado_rem = "excluida"
        elif b.numero in estado.remesas_ajenas or chofer_ajeno:
            estado_rem = "otra_empresa"
        elif b.numero in estado.remesas:
            estado_rem = "existente"
        else:
            estado_rem = "nueva"
        remesas.append({
            "numero": b.numero,
            "numero_generado": b.numero_generado,
            "fila": b.fila,
            "empresa": b.empresa,
            "destino": b.destino,
            "chofer_cedula": b.chofer_cedula,
            "chofer_nombre": b.chofer_nombre,
            "placa": b.placa,
            "fecha_referencia": b.fecha_referencia.isoformat() if b.fecha_referencia else None,
            "facturas": len(b.facturas),
            "facturas_nuevas": nuevas,
            "estado": estado_rem,
            "chofer_registrado": registrado,
            "chofer_otra_empresa": chofer_ajeno,
            "seleccion_sugerida": (not b.excluida) and nuevas > 0 and estado_rem == "nueva",
        })
        if estado_rem == "otra_empresa":
            motivo = "su chofer" if chofer_ajeno else "su número"
            avisos.append(Aviso("REMESA_OTRA_EMPRESA", Nivel.ADVERTENCIA,
                                f"Remesa {b.numero}: {motivo} pertenece a otra empresa de transporte; se omite.",
                                fila=b.fila, remesa=b.numero).to_dict())
        if b.chofer_cedula and not b.excluida and not chofer_ajeno:
            ch = por_chofer.setdefault(b.chofer_cedula, {
                "cedula": b.chofer_cedula, "nombre": b.chofer_nombre, "placa": b.placa, "telefono": b.telefono,
                "registrado": registrado, "remesas": 0, "facturas_nuevas": 0,
            })
            ch["remesas"] += 1
            ch["facturas_nuevas"] += nuevas

    no_reg = [c for c in por_chofer.values() if not c["registrado"]]
    for c in no_reg:
        avisos.append(Aviso("CHOFER_NO_REGISTRADO", Nivel.ADVERTENCIA,
                            f"Chofer {c['nombre'] or ''} (C.I. {c['cedula']}) no está registrado. "
                            "Regístrelo o marque 'registrar choferes nuevos'; si no, sus remesas se "
                            "omiten.").to_dict())
    if archivo_repetido:
        avisos.insert(0, Aviso("ARCHIVO_YA_CARGADO", Nivel.INFO,
                               "Este mismo archivo ya fue confirmado antes. Solo se cargará lo nuevo.").to_dict())

    niveles: dict[str, int] = defaultdict(int)
    for a in avisos:
        niveles[a["nivel"]] += 1
    validas = [r for r in remesas if r["estado"] not in ("excluida", "otra_empresa")]
    todas_clases = [c for d in clas.facturas.values() for c in d.values()]
    return {
        "archivo": archivo,
        "hoja": parseo.hoja,
        "hojas_disponibles": parseo.hojas_disponibles,
        "filas_leidas": parseo.filas_leidas,
        "archivo_repetido": archivo_repetido,
        "mapeo_ia": mapeo_ia,
        # Rango de «Fecha Fac» elegido al analizar (None = toda la hoja) y cuántas facturas quedaron fuera
        "filtro_fecha_fac": parseo.filtro,
        "facturas_detalle": detalle,
        "facturas_detalle_truncado": bool(detalle is not None and parseo.filtro
                                          and parseo.filtro["facturas_en_rango"] > len(detalle)),
        "totales": {
            "remesas": len(validas),
            "remesas_nuevas": sum(1 for r in validas if r["estado"] == "nueva"),
            "remesas_existentes": sum(1 for r in validas if r["estado"] == "existente"),
            "remesas_excluidas": len(remesas) - len(validas),
            "remesas_sugeridas": sum(1 for r in remesas if r["seleccion_sugerida"]),
            "facturas": len(todas_clases),
            "facturas_nuevas": sum(1 for c in todas_clases if c in ("nueva", "reenvio")),
            "facturas_ya_cargadas": sum(1 for c in todas_clases if c == "ya_cargada"),
            "facturas_en_otra_remesa": sum(1 for c in todas_clases if c == "en_otra_remesa"),
            "facturas_en_otra_empresa": sum(1 for c in todas_clases if c == "en_otra_empresa"),
            "choferes": len(por_chofer),
            "choferes_no_registrados": len(no_reg),
        },
        "avisos_por_nivel": dict(niveles),
        "choferes": sorted(por_chofer.values(), key=lambda c: (c["registrado"], c["nombre"] or "")),
        "remesas": remesas,
        "avisos": avisos,
    }


# ---------------------------------------------------------------------- API
class Importador:
    def __init__(self, db: Session, ai: ClaudeClient | None = None) -> None:
        self.db = db
        self.ai = ai

    def vista_previa(
        self,
        contenido: bytes,
        archivo: str,
        usuario: Usuario,
        hoja: str | None = None,
        guardar: bool = True,
        filtro: FiltroFechaFac | None = None,
    ) -> Importacion:
        """Vista previa a partir del archivo Excel completo (servidor propio, CLI).

        ``filtro``: solo las facturas con «Fecha Fac» en el rango; el resto del análisis no cambia."""
        sha = hashlib.sha256(contenido).hexdigest()
        parseo = RemesaParser(filtro=filtro).parse_file(io.BytesIO(contenido), hoja)

        def reparse(alias: dict[str, str]) -> ResultadoParseo:
            return RemesaParser(extra_aliases=alias, filtro=filtro).parse_file(io.BytesIO(contenido), parseo.hoja)

        return self._crear_preview(parseo, reparse, sha, archivo, usuario, guardar)

    def vista_previa_filas(
        self,
        filas: list[list[Any]],
        hoja: str,
        hojas: list[str],
        archivo: str,
        usuario: Usuario,
        sha256: str | None = None,
        guardar: bool = True,
        filtro: FiltroFechaFac | None = None,
    ) -> Importacion:
        """Vista previa a partir de las filas que el navegador extrajo del Excel.

        Así un libro de 9 MB se envía como ~0,5 MB de datos y no choca con el límite de 4,5 MB por
        petición de Vercel. El análisis es exactamente el mismo que con el archivo.
        """
        sha = sha256 or hashlib.sha256(repr(filas).encode("utf-8")).hexdigest()
        parseo = RemesaParser(filtro=filtro).parse_rows(filas, hoja=hoja, hojas=hojas or [hoja])

        def reparse(alias: dict[str, str]) -> ResultadoParseo:
            return RemesaParser(extra_aliases=alias, filtro=filtro).parse_rows(filas, hoja=hoja, hojas=hojas or [hoja])

        return self._crear_preview(parseo, reparse, sha, archivo, usuario, guardar)

    def _crear_preview(
        self,
        parseo: ResultadoParseo,
        reparse: Callable[[dict[str, str]], ResultadoParseo],
        sha: str,
        archivo: str,
        usuario: Usuario,
        guardar: bool = True,
    ) -> Importacion:
        """``guardar=False``: solo análisis (rol Consultor). Devuelve el mismo resumen sin escribir en la
        base: la importación queda con id 0 y estado «analisis», y no se puede confirmar."""
        mapeo_ia: dict[str, str] = {}
        faltantes_idx = sorted({c for b in parseo.bloques for c in b.columnas_faltantes})
        if self.ai is not None and self.ai.enabled and parseo.encabezados_desconocidos and faltantes_idx:
            from tracking.services.remesa_parser import NOMBRES_CAMPO

            inverso = {v: k for k, v in NOMBRES_CAMPO.items()}
            faltantes = [inverso[n] for n in faltantes_idx if n in inverso]
            mapeo_ia = self.ai.mapear_encabezados(parseo.encabezados_desconocidos, faltantes)
            if mapeo_ia:
                log.info("ia_mapeo_encabezados", extra={"mapeo": mapeo_ia})
                parseo = reparse(mapeo_ia)

        estado = _cargar_estado_bd(self.db, parseo.bloques)
        clas = _clasificar(parseo.bloques, estado)
        repetido = self.db.scalar(
            select(Importacion.id).where(
                Importacion.archivo_sha256 == sha, Importacion.estado == EstadoImportacion.CONFIRMADA.value
            )
        ) is not None
        resumen = _resumen(parseo, estado, clas, archivo, repetido, mapeo_ia)

        scope = scope_de_sesion(self.db)
        imp = Importacion(
            # La vista previa contiene datos del Excel: pertenece a la empresa de quien la carga
            empresa_id=scope.empresa_id if scope.restringido else None,
            archivo_nombre=archivo[:255],
            archivo_sha256=sha,
            hoja=parseo.hoja,
            estado=EstadoImportacion.PREVIEW.value,
            creado_por_id=usuario.id,
            resumen=resumen,
            datos={"bloques": [b.to_dict() for b in parseo.bloques]},
        )
        if self.ai is not None and self.ai.enabled:
            imp.revision_ia = self.ai.revisar_importacion(resumen)
        if not guardar:
            imp.id, imp.estado, imp.created_at = 0, "analisis", now_utc()
            log.info("importacion_analisis", extra={"usuario_id": usuario.id, "totales": resumen["totales"]})
            return imp
        self.db.add(imp)
        self.db.commit()
        log.info("importacion_preview", extra={"importacion_id": imp.id, "totales": resumen["totales"]})
        return imp

    def descartar(self, imp: Importacion) -> None:
        if imp.estado != EstadoImportacion.PREVIEW.value:
            raise ImportacionError("Solo se puede descartar una vista previa pendiente", 409)
        imp.estado = EstadoImportacion.DESCARTADA.value
        self.db.commit()

    def confirmar(
        self,
        imp: Importacion,
        usuario: Usuario,
        remesas_sel: list[str] | None,
        fecha_desde: date | None,
        registrar_choferes: bool,
        empresa_choferes: int | None = None,
    ) -> dict[str, Any]:
        """Confirma la carga. ``empresa_choferes``: empresa de los choferes que se registren aquí
        (el endpoint la resuelve: la del Transportista o la indicada por el Supervisor)."""
        if imp.estado != EstadoImportacion.PREVIEW.value:
            raise ImportacionError("Esta importación ya fue confirmada o descartada", 409)

        bloques = [BloqueRemesa.from_dict(d) for d in imp.datos.get("bloques", [])]
        bloques = [b for b in bloques if not b.excluida and b.numero]
        if remesas_sel:
            wanted = set(remesas_sel)
            bloques = [b for b in bloques if b.numero in wanted]
        else:
            sugeridas = {r["numero"] for r in imp.resumen.get("remesas", []) if r.get("seleccion_sugerida")}
            bloques = [b for b in bloques if b.numero in sugeridas]
        if fecha_desde:
            bloques = [b for b in bloques if b.fecha_referencia and b.fecha_referencia >= fecha_desde]
        if not bloques:
            raise ImportacionError("No hay remesas seleccionadas para importar")

        # Re-evaluar contra la BD actual (pudo cambiar desde la vista previa)
        estado = _cargar_estado_bd(self.db, bloques)
        clas = _clasificar(bloques, estado)

        credenciales: list[dict[str, str]] = []
        excluidas: list[str] = []
        if registrar_choferes:
            for b in bloques:
                ced = b.chofer_cedula
                if ced and ced not in estado.conductores and ced not in estado.conductores_ajenos:
                    conductor, clave = crear_usuario_conductor(
                        self.db, cedula=ced, nombre=b.chofer_nombre or f"Chofer {ced}",
                        telefono=b.telefono, placa=b.placa, empresa_id=empresa_choferes,
                    )
                    estado.conductores[ced] = conductor
                    credenciales.append({"usuario": ced, "nombre": conductor.nombre, "clave_temporal": clave or ""})

        ahora = now_utc()
        rem_creadas = rem_actualizadas = fac_creadas = fac_omitidas = 0
        for b in bloques:
            asignado = estado.conductores.get(b.chofer_cedula or "")
            if asignado is None or (b.numero or "") in estado.remesas_ajenas:
                excluidas.append(b.numero or "?")
                continue
            remesa = estado.remesas.get(b.numero or "")
            if remesa is None:
                remesa = Remesa(
                    empresa_id=_empresa_conductor(asignado),
                    numero=b.numero, numero_generado=b.numero_generado, empresa=b.empresa,
                    destino=b.destino, conductor_id=asignado.id, chofer_cedula=b.chofer_cedula,
                    chofer_nombre=b.chofer_nombre, chofer_telefono=b.telefono, placa=b.placa,
                    importacion_id=imp.id, fila_origen=b.fila,
                )
                self.db.add(remesa)
                self.db.flush()
                estado.remesas[remesa.numero] = remesa
                rem_creadas += 1
            else:
                rem_actualizadas += 1
            clases = clas.facturas.get(b.numero or "", {})
            for f in b.facturas:
                if clases.get(f.numero) not in ("nueva", "reenvio"):
                    fac_omitidas += 1
                    continue
                # Fecha Preparado = la «Fecha Alm» del Excel (solo día → 00:00 locales); si el Excel no la
                # trae, el momento de la carga. Es también la fecha del evento «Preparado» del historial.
                # Fecha Entregado (fecha_rec) NO sale del Excel: se llena cuando el conductor marca Entregada.
                preparado = min(inicio_dia_local(f.fecha_alm) or ahora, ahora)
                factura = Factura(
                    remesa_id=remesa.id, conductor_id=remesa.conductor_id, empresa=b.empresa,
                    empresa_id=remesa.empresa_id,
                    numero=f.numero, pedido=f.pedido, cod_vendedor=f.cod_vendedor, cliente=f.cliente,
                    fecha_fac=f.fecha_fac, fecha_alm=preparado, bultos=f.bultos,
                    total_fact=f.total_fact, porcentaje=f.porcentaje, total_neto=f.total_neto,
                    flete_pct=f.flete_pct, total_flete=f.total_flete, comentario=f.comentario,
                    fila_origen=f.fila, estatus=Estatus.RECIBIDA.value, estatus_at=preparado,
                )
                # Reingreso: la factura venía de una devolución → nuevo ciclo enlazado con el anterior
                previa = (factura_devuelta_previa(self.db, f.numero, b.empresa)
                          if clases.get(f.numero) == "reenvio" else None)
                factura.reingreso_de_id = previa.id if previa else None
                self.db.add(factura)
                self.db.flush()
                self.db.add(EventoEstatus(
                    id=str(uuid.uuid4()), factura_id=factura.id, estatus=Estatus.RECIBIDA.value,
                    estatus_anterior=None, ocurrido_at=preparado, recibido_at=ahora, usuario_id=usuario.id,
                    origen=OrigenEvento.IMPORTACION.value, aplicado=True,
                    motivo=_nota_reingreso(previa),
                ))
                # Evita duplicar si la misma factura aparece en dos remesas del mismo lote
                clave = clave_factura(b.empresa, f.numero)
                estado.facturas.setdefault(clave, []).append((remesa.numero, Estatus.RECIBIDA.value, False))
                fac_creadas += 1

        resultado = {
            "remesas_creadas": rem_creadas,
            "remesas_actualizadas": rem_actualizadas,
            "facturas_creadas": fac_creadas,
            "facturas_omitidas": fac_omitidas,
            "remesas_excluidas": excluidas,
            "choferes_registrados": [c["usuario"] for c in credenciales],
        }
        imp.estado = EstadoImportacion.CONFIRMADA.value
        imp.confirmado_at = ahora
        imp.resultado = resultado  # sin claves temporales: no se guardan en texto plano
        self.db.commit()
        log.info("importacion_confirmada", extra={"importacion_id": imp.id, **resultado})
        return {**resultado, "importacion_id": imp.id, "credenciales": credenciales}

