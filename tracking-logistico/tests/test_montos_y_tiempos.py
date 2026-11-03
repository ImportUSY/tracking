"""v1.17.0: montos de la factura solo para SUPERVISOR y COBRANZAS, y días transcurridos entre procesos."""

from __future__ import annotations

import io
import json
from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from tests.conftest import login
from tests.test_flujo_completo import _cargar
from tests.test_multitenant import _usuario
from tracking.enums import Estatus
from tracking.models import Factura
from tracking.services.tiempos import dias_entre_procesos, formato_duracion, minutos_habiles, minutos_por_dia

MONTOS = ("total_fact", "porcentaje", "total_neto", "flete_pct", "total_flete")


def _consultor(client: TestClient, sup: dict[str, str]) -> dict[str, str]:
    r = client.post("/api/admin/usuarios", headers=sup,
                    json={"username": "consul9", "nombre": "Consul", "rol": "consultor"})
    assert r.status_code == 201, r.text
    return login(client, "consul9", r.json()["credencial"]["clave_temporal"])


def test_montos_solo_supervisor_y_cobranzas(client: TestClient, sup: dict[str, str]) -> None:
    ctx = _cargar(client, sup)
    client.post(f"/api/importaciones/{ctx['preview']['id']}/confirmar", headers=sup, json={})
    roles = {
        "supervisor": sup,
        "cobranzas": _usuario(client, sup, "cobra1", "cobranzas", None),
        "transportista": _usuario(client, sup, "transp1", "transportista", ctx["empresa"]),
        "consultor": _consultor(client, sup),
    }
    for rol, h in roles.items():
        ve = rol in ("supervisor", "cobranzas")
        lista = client.get("/api/facturas", headers=h).json()["items"]
        assert lista, rol
        det = client.get(f"/api/facturas/{lista[0]['id']}", headers=h).json()
        rem = client.get(f"/api/remesas/{det['remesa_id']}", headers=h).json()
        for f in [*lista, det, *rem["facturas"]]:
            assert f["montos_visibles"] is ve, rol
            assert (f["total_fact"] is not None) is ve and (f["total_neto"] is not None) is ve, rol
            if not ve:
                assert all(f[k] is None for k in MONTOS), rol
        # Reporte de Cobranzas (pantalla y Excel): sin columnas ni totales de montos
        params = {"remesa_ids": [det["remesa_id"]]}
        tabla = client.get("/api/reportes/cobranzas", headers=h, params=params).json()
        claves = {c["clave"] for c in tabla["columnas"]}
        assert ("total_fact" in claves) is ve and ("total_fact" in tabla["totales"]) is ve, rol
        if not ve:
            assert not any(k in json.dumps(tabla["items"]) for k in MONTOS), rol
        xlsx = client.get("/api/reportes/cobranzas.xlsx", headers=h, params=params)
        assert xlsx.status_code == 200, rol
        ws = load_workbook(io.BytesIO(xlsx.content)).active
        assert ws is not None
        valores = {c.value for fila in ws.iter_rows() for c in fila}
        assert ("Total Fact" in valores) is ve and ("Total Neto" in valores) is ve, rol
        # La interfaz: el Consultor ve los botones del Supervisor, pero no los montos
        perm = client.get("/api/me/permisos", headers=h).json()
        assert ("facturas.montos.ver" in perm["permisos_interfaz"]) is ve, rol


def _f(estatus: Estatus, **kw: object) -> Factura:
    return Factura(numero="1", estatus=estatus.value, **kw)


def _vet(y: int, m: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    """Hora de Caracas (UTC-4) → UTC."""
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc) + timedelta(hours=4)


def test_minutos_de_jornada_laboral() -> None:
    # Jornada: lunes a viernes de 7:00 a 16:00 (día hábil = 9 h); noches y fines de semana no cuentan
    assert minutos_por_dia() == 540
    # Viernes 23-oct 15:00 → lunes 26-oct 09:00: 1 h del viernes + 2 h del lunes
    assert minutos_habiles(_vet(2026, 10, 23, 15), _vet(2026, 10, 26, 9)) == 180
    # Viernes después de las 16:00 → lunes antes de las 7:00: nada
    assert minutos_habiles(_vet(2026, 10, 23, 18), _vet(2026, 10, 26, 6)) == 0
    # Todo el fin de semana: 0
    assert minutos_habiles(_vet(2026, 10, 24, 8), _vet(2026, 10, 25, 14)) == 0
    # Mismo día dentro de la jornada, y recortado a la jornada
    assert minutos_habiles(_vet(2026, 10, 20, 8, 15), _vet(2026, 10, 20, 11, 45)) == 210
    assert minutos_habiles(_vet(2026, 10, 20, 5), _vet(2026, 10, 20, 6, 30)) == 0
    assert minutos_habiles(_vet(2026, 10, 20, 15), _vet(2026, 10, 20, 20)) == 60
    # Una semana completa = 5 días hábiles de 9 h
    assert minutos_habiles(_vet(2026, 10, 19), _vet(2026, 10, 26)) == 5 * 540
    # Al revés: negativo
    assert minutos_habiles(_vet(2026, 10, 26, 9), _vet(2026, 10, 23, 15)) == -180
    # Formato con días de 9 h; menos de un día: solo horas y minutos
    assert formato_duracion(540) == "1 día 0 h 00 min" and formato_duracion(605) == "1 día 1 h 05 min"
    assert formato_duracion(185) == "3 h 05 min" and formato_duracion(0) == "0 h 00 min"
    assert formato_duracion(5 * 540 + 7) == "5 días 0 h 07 min"


def test_dias_entre_procesos() -> None:
    ahora = _vet(2026, 10, 26, 10)  # lunes 10:00
    # Entregada. Factura jue 22-oct → Preparado jue 08:30 → Por Entregar vie 14:00 → Entregado lun 09:15
    t = dias_entre_procesos(_f(Estatus.ENTREGADA, fecha_fac=date(2026, 10, 22), fecha_alm=_vet(2026, 10, 22, 8, 30),
                               fecha_tra=_vet(2026, 10, 23, 14), fecha_rec=_vet(2026, 10, 26, 9, 15)), ahora)
    # jue 7:00–8:30 · jue 8:30–16:00 + vie 7:00–14:00 · vie 14:00–16:00 + lun 7:00–9:15
    assert [x.texto for x in t.tramos] == ["1 h 30 min", "1 día 5 h 30 min", "4 h 15 min"]
    assert t.total_minutos == 90 + 870 + 255 and t.total_texto == "2 días 2 h 15 min"
    assert t.entregada and not t.en_curso and not t.devolucion and t.mensaje is None
    assert t.nota == "Días hábiles: de lunes a viernes de 7:00 a 16:00"
    assert [(x.desde, x.hasta) for x in t.tramos] == [("Fecha Factura", "Fecha Preparado"),
                                                       ("Fecha Preparado", "Fecha Por Entregar"),
                                                       ("Fecha Por Entregar", "Fecha Entregado")]
    # Por Entregar (vie 20:00, fuera de jornada): el tramo actual cuenta hasta ahora → lunes 7:00–10:00
    t = dias_entre_procesos(_f(Estatus.EN_TRANSITO, fecha_fac=date(2026, 10, 22), fecha_alm=_vet(2026, 10, 22),
                               fecha_tra=_vet(2026, 10, 23, 20)), ahora)
    assert [x.estado for x in t.tramos] == ["completo", "completo", "en_curso"]
    assert t.tramos[2].texto == "3 h 00 min" and t.en_curso and not t.entregada
    # Preparado: espera la salida; el último tramo queda pendiente
    t = dias_entre_procesos(_f(Estatus.RECIBIDA, fecha_fac=date(2026, 10, 22), fecha_alm=_vet(2026, 10, 26, 8)), ahora)
    assert [x.estado for x in t.tramos] == ["completo", "en_curso", "pendiente"] and t.tramos[1].minutos == 120
    # Devolución: los mismos tramos que una entrega; el último termina en la devolución
    t = dias_entre_procesos(_f(Estatus.DEVOLUCION, fecha_fac=date(2026, 10, 20), fecha_alm=_vet(2026, 10, 20, 9),
                               fecha_tra=_vet(2026, 10, 23, 16), estatus_at=_vet(2026, 10, 26, 8, 41)), ahora)
    assert t.devolucion and t.mensaje == "No se entregó - Devolución" and not t.entregada and not t.en_curso
    assert [(x.hasta, x.texto) for x in t.tramos] == [
        ("Fecha Preparado", "2 h 00 min"), ("Fecha Por Entregar", "3 días 7 h 00 min"),
        ("Fecha Devolución", "1 h 41 min")]
    assert t.total_minutos == 120 + 2040 + 101 and t.total_texto == "4 días 1 h 41 min"
    # Falta una fecha intermedia (factura vieja sin Fecha Preparado) y fechas fuera de orden
    t = dias_entre_procesos(_f(Estatus.ENTREGADA, fecha_fac=date(2026, 10, 20), fecha_tra=_vet(2026, 10, 22),
                               fecha_rec=_vet(2026, 10, 23)), ahora)
    assert [x.estado for x in t.tramos] == ["sin_dato", "sin_dato", "completo"] and t.total_minutos == 540 and t.aviso
    t = dias_entre_procesos(_f(Estatus.ENTREGADA, fecha_fac=date(2026, 10, 20), fecha_alm=_vet(2026, 10, 19),
                               fecha_tra=_vet(2026, 10, 22), fecha_rec=_vet(2026, 10, 23)), ahora)
    assert t.tramos[0].minutos == -540 and t.aviso and "fuera de orden" in t.aviso
