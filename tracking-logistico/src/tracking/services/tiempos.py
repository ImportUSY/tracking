"""Tiempo transcurrido entre los procesos de una factura (motor de SLA).

Hitos, en orden:  Fecha Factura → Fecha Preparado → Fecha Por Entregar → Fecha Entregado

Reglas de negocio:

* **Tiempo hábil = jornada laboral**: solo cuenta el tiempo de lunes a viernes dentro del horario de trabajo
  (por defecto de 7:00 a 16:00, hora local de ``TIMEZONE``; ``JORNADA_INICIO`` / ``JORNADA_FIN``).
  Un **día hábil = 9 horas**. Lo que ocurre de noche, antes de las 7:00, después de las 16:00, en fin de
  semana o en un **día feriado** (tabla ``feriados``) no suma.
  Ejemplo: viernes 15:00 → lunes 9:00 = 1 h del viernes + 2 h del lunes = 3 h.
* Se muestra «2 días 3 h 05 min» (días de 9 h); si no llega a un día, solo «3 h 05 min» (nunca «0 días»).
* Fecha Factura solo trae el día (igual que las fechas del Excel): cuenta desde las 00:00 de ese día, es
  decir, desde el inicio de esa jornada.
* El tramo que está ocurriendo ahora (inicio conocido, fin aún no) cuenta hasta este momento y se
  marca «en curso»; los siguientes quedan «pendiente».
* **Devolución**: no hubo entrega. Se muestran los mismos tramos que en una entrega, pero el último
  termina en el momento en que se registró la devolución («Fecha Devolución»), con el mensaje
  «No se entregó - Devolución». El total suma los tres tramos.
* **No Entregado** (cliente no disponible): la entrega sigue pendiente; el último tramo sigue en curso y
  se muestra el mensaje «No se entregó - Cliente no Disponible».
* Si falta una fecha intermedia (p. ej. factura vieja sin Fecha Preparado) el tramo queda «sin_dato».
* «Total para la entrega» = suma aritmética de los tramos (en minutos hábiles).
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time

from tracking.config import get_settings
from tracking.enums import MENSAJE_NO_ENTREGADO, Estatus
from tracking.models import Factura
from tracking.services.timeutil import local_tz, now_utc, to_local

HITOS: tuple[tuple[str, str], ...] = (
    ("fecha_fac", "Fecha Factura"),
    ("fecha_alm", "Fecha Preparado"),
    ("fecha_tra", "Fecha Por Entregar"),
    ("fecha_rec", "Fecha Entregado"),
)
MENSAJE_DEVOLUCION = "No se entregó - Devolución"
_LUNES = date(2000, 1, 3)  # un lunes cualquiera como origen del conteo


def jornada() -> tuple[int, int]:
    """(inicio, fin) de la jornada en segundos desde la medianoche local. Por defecto 7:00 y 16:00."""
    s = get_settings()
    return s.jornada_inicio * 3600, s.jornada_fin * 3600


def minutos_por_dia() -> int:
    ini, fin = jornada()
    return (fin - ini) // 60


def texto_jornada() -> str:
    """«Días hábiles: de lunes a viernes de 7:00 a 16:00» (según la configuración)."""
    s = get_settings()
    return f"Días hábiles: de lunes a viernes de {s.jornada_inicio}:00 a {s.jornada_fin}:00"


@dataclass(frozen=True)
class Tramo:
    desde: str
    hasta: str
    inicio: datetime | None
    fin: datetime | None
    minutos: int | None
    texto: str | None
    estado: str  # completo | en_curso | pendiente | sin_dato


@dataclass(frozen=True)
class Tiempos:
    tramos: list[Tramo]
    total_minutos: int | None
    total_texto: str | None
    entregada: bool
    en_curso: bool
    devolucion: bool
    mensaje: str | None
    aviso: str | None
    nota: str | None = None


def _segundos_habiles_desde_origen(momento: datetime, feriados: Sequence[date] = ()) -> float:
    """Segundos de jornada (lunes a viernes, dentro del horario) entre el lunes de origen y ``momento``."""
    ini, fin = jornada()
    largo = fin - ini
    local = to_local(momento).replace(tzinfo=None)
    dias = (local.date() - _LUNES).days
    semanas, dia_semana = divmod(dias, 7)  # 0 = lunes … 5 = sábado, 6 = domingo
    seg = float(semanas * 5 * largo + min(dia_semana, 5) * largo)
    # Feriados de lunes a viernes (ordenados): cada uno anterior a este día resta una jornada completa
    seg -= bisect_left(feriados, local.date()) * largo
    if dia_semana < 5 and not _es_feriado(feriados, local.date()):
        del_dia = (local - datetime.combine(local.date(), time.min)).total_seconds()
        seg += min(max(del_dia - ini, 0.0), float(largo))  # antes de las 7:00 = 0; después de las 16:00 = 9 h
    return seg


def _es_feriado(feriados: Sequence[date], dia: date) -> bool:
    i = bisect_left(feriados, dia)
    return i < len(feriados) and feriados[i] == dia


def minutos_habiles(inicio: datetime, fin: datetime, feriados: Sequence[date] = ()) -> int:
    """Minutos de jornada laboral entre dos instantes (sin noches ni fines de semana). Negativo si
    ``fin`` es anterior a ``inicio``."""
    seg = _segundos_habiles_desde_origen(fin, feriados) - _segundos_habiles_desde_origen(inicio, feriados)
    return int(seg // 60) if seg >= 0 else -int(-seg // 60)


def formato_duracion(minutos: int | None) -> str | None:
    """Con días de 9 h: 605 → «1 día 1 h 05 min»; 185 → «3 h 05 min» (sin «0 días»)."""
    if minutos is None:
        return None
    signo, m = ("-" if minutos < 0 else ""), abs(minutos)
    dias, resto = divmod(m, minutos_por_dia())
    horas, mins = divmod(resto, 60)
    hm = f"{horas} h {mins:02d} min"
    if dias:
        return f"{signo}{dias} día{'s' if dias != 1 else ''} {hm}"
    return f"{signo}{hm}"


def _momento(valor: date | datetime | None) -> datetime | None:
    if valor is None or isinstance(valor, datetime):
        return valor
    return datetime.combine(valor, time.min, tzinfo=local_tz())  # solo el día: desde las 00:00 locales


def _tramo(desde: str, hasta: str, inicio: datetime, fin: datetime, estado: str = "completo",
           feriados: Sequence[date] = ()) -> Tramo:
    minutos = minutos_habiles(inicio, fin, feriados)
    if estado == "en_curso":
        minutos = max(minutos, 0)
    fin_visible = fin if estado == "completo" else None
    return Tramo(desde, hasta, inicio, fin_visible, minutos, formato_duracion(minutos), estado)


def dias_entre_procesos(f: Factura, ahora: datetime | None = None, feriados: Sequence[date] = ()) -> Tiempos:
    """``feriados``: fechas de lunes a viernes, ordenadas (``services.feriados.fechas``)."""
    ahora = ahora or now_utc()
    fechas = [_momento(getattr(f, campo)) for campo, _ in HITOS]
    nombres = [nombre for _, nombre in HITOS]
    devolucion = f.estatus == Estatus.DEVOLUCION.value
    if devolucion:
        # Mismos tramos que una entrega, pero el último termina en el registro de la devolución
        fechas[3], nombres[3] = f.estatus_at, "Fecha Devolución"
    tramos: list[Tramo] = []
    abierto = False  # ya hay un tramo en curso: los siguientes esperan
    for i in range(len(HITOS) - 1):
        inicio, fin = fechas[i], fechas[i + 1]
        desde, hasta = nombres[i], nombres[i + 1]
        posterior = any(x is not None for x in fechas[i + 2:])  # ya ocurrió un hito más adelante
        if inicio is not None and fin is not None:
            tramos.append(_tramo(desde, hasta, inicio, fin, feriados=feriados))
        elif (inicio is None and fin is not None) or (inicio is not None and fin is None and posterior):
            tramos.append(Tramo(desde, hasta, inicio, fin, None, None, "sin_dato"))
        elif inicio is not None and fin is None and not abierto:
            tramos.append(_tramo(desde, hasta, inicio, ahora, "en_curso", feriados))
            abierto = True
        else:
            tramos.append(Tramo(desde, hasta, inicio, fin, None, None, "pendiente"))
    mensaje = MENSAJE_DEVOLUCION if devolucion else None
    if f.estatus == Estatus.NO_ENTREGADO.value:
        mensaje = MENSAJE_NO_ENTREGADO
    return _resumen(tramos, devolucion=devolucion, mensaje=mensaje)


def _resumen(tramos: list[Tramo], devolucion: bool, mensaje: str | None = None) -> Tiempos:
    contados = [t.minutos for t in tramos if t.minutos is not None]
    total = sum(contados) if contados else None
    aviso = None
    if any(t.minutos is not None and t.minutos < 0 for t in tramos):
        aviso = "Hay fechas fuera de orden (un hito anterior al previo): revise las fechas de la factura."
    elif any(t.estado == "sin_dato" for t in tramos):
        aviso = "Falta alguna fecha intermedia: el total solo suma los tramos que se pueden calcular."
    return Tiempos(
        tramos=tramos,
        total_minutos=total,
        total_texto=formato_duracion(total),
        entregada=not devolucion and all(t.estado == "completo" for t in tramos),
        en_curso=any(t.estado == "en_curso" for t in tramos),
        devolucion=devolucion,
        mensaje=mensaje,
        aviso=aviso,
        nota=texto_jornada(),
    )

