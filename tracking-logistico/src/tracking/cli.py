"""Línea de comandos de operación: servidor, migraciones, usuarios y cargas desde Excel."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import date
from pathlib import Path

from sqlalchemy import select

from tracking.config import get_settings
from tracking.db import Database
from tracking.enums import Rol
from tracking.logging_config import configure_logging
from tracking.models import Usuario


def _db() -> Database:
    s = get_settings()
    configure_logging(s.log_level, as_json=False)
    db = Database(s)
    db.wait_until_ready()
    return db


def cmd_serve(a: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("tracking.main:app_factory", factory=True, host=a.host, port=a.port, reload=a.reload,
                workers=None if a.reload else a.workers, proxy_headers=True, forwarded_allow_ips="*")
    return 0


def cmd_migrate(_: argparse.Namespace) -> int:
    from tracking.migrate import upgrade_head

    upgrade_head(_db().engine)
    print("Base de datos actualizada.")
    return 0


def cmd_crear_supervisor(a: argparse.Namespace) -> int:
    from tracking.migrate import upgrade_head
    from tracking.security import hash_password
    from tracking.services.users import crear_usuario

    db = _db()
    upgrade_head(db.engine)
    clave = a.clave or getpass.getpass("Clave del supervisor: ")
    if len(clave) < 6:
        print("La clave debe tener al menos 6 caracteres", file=sys.stderr)
        return 1
    with db.session() as s:
        existente = s.scalar(select(Usuario).where(Usuario.username == a.usuario))
        if existente:
            existente.password_hash = hash_password(clave)
            existente.rol, existente.activo = Rol.SUPERVISOR.value, True
            existente.token_version += 1
            print(f"Usuario '{a.usuario}' actualizado como supervisor.")
        else:
            crear_usuario(s, username=a.usuario, nombre=a.nombre, rol=Rol.SUPERVISOR, clave=clave)
            print(f"Supervisor '{a.usuario}' creado.")
        s.commit()
    return 0


def cmd_cargar_vendedores(a: argparse.Namespace) -> int:
    from tracking.migrate import upgrade_head
    from tracking.services.catalogos import importar_vendedores

    db = _db()
    upgrade_head(db.engine)
    with db.session() as s:
        r = importar_vendedores(s, Path(a.archivo))
    print(f"Vendedores: {r.creados} creados, {r.actualizados} actualizados, {r.omitidos} omitidos")
    return 0


def cmd_cargar_placas(a: argparse.Namespace) -> int:
    from tracking.migrate import upgrade_head
    from tracking.services.catalogos import importar_placas

    db = _db()
    upgrade_head(db.engine)
    with db.session() as s:
        r = importar_placas(s, Path(a.archivo), a.hoja, empresa_id=a.empresa)
    print(f"Choferes/camiones: {r.creados} creados, {r.actualizados} actualizados, {r.omitidos} omitidos")
    for d in r.detalle:
        print("  -", d)
    if r.credenciales:
        print("\nClaves temporales (entréguelas a cada conductor; se pedirá cambiarlas al entrar):")
        for c in r.credenciales:
            print(f"  {c['usuario']:<12} {c['nombre']:<35} {c['clave_temporal']}")
    return 0


def cmd_importar_remesa(a: argparse.Namespace) -> int:
    from tracking.migrate import upgrade_head
    from tracking.services.importer import Importador

    db = _db()
    upgrade_head(db.engine)
    with db.session() as s:
        actor = s.scalar(select(Usuario).where(Usuario.rol == Rol.SUPERVISOR.value).order_by(Usuario.id))
        if actor is None:
            print("Cree primero un supervisor (tracking crear-supervisor)", file=sys.stderr)
            return 1
        contenido = Path(a.archivo).read_bytes()
        imp = Importador(s).vista_previa(contenido, Path(a.archivo).name, actor, a.hoja)
        t = imp.resumen["totales"]
        print(json.dumps(t, indent=2, ensure_ascii=False))
        for av in imp.resumen["avisos"][: a.max_avisos]:
            print(f"[{av['nivel']:<11}] {av['codigo']:<26} {av['mensaje']}")
        if not a.confirmar:
            print(f"\nVista previa #{imp.id} guardada. Use --confirmar para cargar.")
            return 0
        desde = date.fromisoformat(a.desde) if a.desde else None
        r = Importador(s).confirmar(imp, actor, None, desde, a.registrar_choferes, a.empresa)
        print(json.dumps({k: v for k, v in r.items() if k != "credenciales"}, indent=2, ensure_ascii=False))
        for c in r["credenciales"]:
            print(f"  {c['usuario']:<12} {c['nombre']:<35} {c['clave_temporal']}")
    return 0


def cmd_archivar_bitacora(a: argparse.Namespace) -> int:
    """Archiva un mes, un año o, con --automatico, todos los meses cerrados salvo los últimos N."""
    from tracking.migrate import upgrade_head
    from tracking.services import bitacora_archivo as arch

    db = _db()
    upgrade_head(db.engine)
    with db.session() as s:
        if a.automatico is not None:
            pendientes = [p for p in arch.pendientes(s) if p["tipo"] == "mes"]
            objetivos = pendientes[: max(0, len(pendientes) - a.automatico)] if a.automatico else pendientes
            periodos = [("mes", p["periodo"]) for p in objetivos]
        else:
            periodos = [("mes", a.mes)] if a.mes else [("anio", a.anio)]
        if not periodos:
            print("No hay meses cerrados pendientes de archivar.")
        for tipo, clave in periodos:
            try:
                r = arch.archivar(s, tipo, clave, None)
                print(f"Archivado {r.periodo}: {r.filas} registros, {r.bytes / 1024:.0f} KB (sha256 {r.sha256[:12]}…)")
            except arch.ArchivoInvalido as exc:
                print(f"{clave}: {exc}", file=sys.stderr)
                if a.automatico is None:
                    return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tracking", description="Tracking Logístico")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("serve", help="Inicia el servidor web")
    sp.add_argument("--host", default="0.0.0.0")
    sp.add_argument("--port", type=int, default=8000)
    sp.add_argument("--workers", type=int, default=1)
    sp.add_argument("--reload", action="store_true")
    sp.set_defaults(func=cmd_serve)

    sub.add_parser("migrate", help="Aplica migraciones de base de datos").set_defaults(func=cmd_migrate)

    sp = sub.add_parser("crear-supervisor", help="Crea o restablece un supervisor")
    sp.add_argument("--usuario", default="supervisor")
    sp.add_argument("--nombre", default="Supervisor")
    sp.add_argument("--clave")
    sp.set_defaults(func=cmd_crear_supervisor)

    sp = sub.add_parser("cargar-vendedores", help="Carga el Listado de Código de Vendedores (.xlsx)")
    sp.add_argument("archivo")
    sp.set_defaults(func=cmd_cargar_vendedores)

    sp = sub.add_parser("cargar-placas", help="Carga choferes y camiones desde la hoja PLACAS")
    sp.add_argument("archivo")
    sp.add_argument("--hoja", default="PLACAS")
    sp.add_argument("--empresa", type=int, help="ID de la empresa de transporte de los choferes nuevos")
    sp.set_defaults(func=cmd_cargar_placas)

    sp = sub.add_parser("archivar-bitacora", help="Archiva la bitácora de un mes o un año ya terminado")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--mes", help="Mes a archivar, AAAA-MM (ej. 2026-09)")
    g.add_argument("--anio", help="Año a archivar, AAAA (ej. 2025)")
    g.add_argument("--automatico", type=int, metavar="N",
                   help="Archiva todos los meses cerrados dejando los últimos N en la bitácora activa")
    sp.set_defaults(func=cmd_archivar_bitacora)

    sp = sub.add_parser("importar-remesa", help="Analiza (y opcionalmente confirma) una hoja REMESA")
    sp.add_argument("archivo")
    sp.add_argument("--hoja")
    sp.add_argument("--confirmar", action="store_true")
    sp.add_argument("--registrar-choferes", action="store_true")
    sp.add_argument("--empresa", type=int, help="ID de la empresa de transporte de los choferes que se registren")
    sp.add_argument("--desde", help="Solo remesas con fecha >= AAAA-MM-DD")
    sp.add_argument("--max-avisos", type=int, default=40)
    sp.set_defaults(func=cmd_importar_remesa)

    args = p.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
