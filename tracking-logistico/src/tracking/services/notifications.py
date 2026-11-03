"""Correos a vendedores (solo estatus) y a Cobranzas (Excel adjunto). Apagado por defecto.

El contenido lo arma el programa a partir de la base de datos; la IA (si está activa) solo
redacta el saludo inicial y nunca agrega datos.
"""

from __future__ import annotations

import html
import logging
import smtplib
import ssl
from collections.abc import Sequence
from dataclasses import dataclass, field
from email.message import EmailMessage

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from tracking.config import Settings
from tracking.enums import Estatus
from tracking.models import Factura, Vendedor
from tracking.services.ai import ClaudeClient
from tracking.services.timeutil import to_local

log = logging.getLogger(__name__)


class CorreoNoConfigurado(RuntimeError):
    pass


@dataclass
class Correo:
    para: list[str]
    asunto: str
    texto: str
    html: str
    adjuntos: list[tuple[str, bytes, str]] = field(default_factory=list)  # (nombre, datos, mime)


def _smtp_transitorio(exc: BaseException) -> bool:
    """Reintenta solo fallas de red y respuestas 4xx (temporales); nunca errores de autenticación."""
    if isinstance(exc, smtplib.SMTPResponseException):
        return 400 <= exc.smtp_code < 500
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return True
    if isinstance(exc, smtplib.SMTPException):
        return False  # destinatarios rechazados, etc.: reintentar no ayuda
    return isinstance(exc, OSError)  # red caída, timeout, conexión rechazada


class EmailSender:
    def __init__(self, settings: Settings) -> None:
        self.s = settings

    def verificar(self) -> None:
        if not self.s.email_enabled:
            raise CorreoNoConfigurado("El envío de correos está desactivado (EMAIL_ENABLED=false)")
        if not self.s.smtp_host or not self.s.smtp_from:
            raise CorreoNoConfigurado("Falta configurar SMTP_HOST y SMTP_FROM")

    def _build(self, correo: Correo) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = self.s.smtp_from
        msg["To"] = ", ".join(correo.para)
        msg["Subject"] = correo.asunto
        msg.set_content(correo.texto)
        msg.add_alternative(correo.html, subtype="html")
        for nombre, datos, mime in correo.adjuntos:
            main, sub = mime.split("/", 1)
            msg.add_attachment(datos, maintype=main, subtype=sub, filename=nombre)
        return msg

    @retry(
        retry=retry_if_exception(_smtp_transitorio),
        stop=stop_after_attempt(4),
        wait=wait_exponential_jitter(initial=2, max=30),
        reraise=True,
    )
    def enviar(self, correo: Correo) -> None:
        self.verificar()
        msg = self._build(correo)
        ctx = ssl.create_default_context()
        if self.s.smtp_ssl:
            with smtplib.SMTP_SSL(self.s.smtp_host, self.s.smtp_port, context=ctx, timeout=30) as smtp:
                self._login_send(smtp, msg)
        else:
            with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=30) as smtp:
                if self.s.smtp_starttls:
                    smtp.starttls(context=ctx)
                self._login_send(smtp, msg)
        log.info("correo_enviado", extra={"para": correo.para, "asunto": correo.asunto})

    def _login_send(self, smtp: smtplib.SMTP, msg: EmailMessage) -> None:
        if self.s.smtp_user:
            smtp.login(self.s.smtp_user, self.s.smtp_password)
        smtp.send_message(msg)


def _fecha(f: Factura) -> str:
    return f"{to_local(f.estatus_at):%d/%m/%Y %H:%M}" if f.estatus_at else ""


def correo_vendedor(
    vendedor: Vendedor | None,
    codigo: str,
    facturas: Sequence[Factura],
    modo: str,
    ai: ClaudeClient | None = None,
) -> Correo | None:
    if vendedor is None or not vendedor.email:
        return None
    titulo = "facturas por entregar" if modo == "en_curso" else "facturas con estatus final"
    saludo = None
    if ai is not None and ai.enabled:
        saludo = ai.redactar_intro_correo(vendedor.nombre, modo, len(facturas))
    saludo = saludo or (
        f"Hola {vendedor.nombre.title()}, le compartimos el estatus actualizado de sus {titulo}."
    )
    filas_txt = [
        f"- {f.numero} | Pedido {f.pedido or '-'} | {f.cliente or '-'} | {Estatus(f.estatus).label} | {_fecha(f)}"
        for f in facturas
    ]
    filas_html = "".join(
        "<tr>"
        + "".join(
            f"<td style='padding:4px 8px;border:1px solid #ddd'>{html.escape(str(v))}</td>"
            for v in (f.numero, f.pedido or "-", f.cliente or "-", Estatus(f.estatus).label, _fecha(f))
        )
        + "</tr>"
        for f in facturas
    )
    cuerpo_html = (
        f"<p>{html.escape(saludo)}</p>"
        "<table style='border-collapse:collapse;font-family:Arial;font-size:13px'>"
        "<tr>" + "".join(f"<th style='padding:4px 8px;border:1px solid #ddd;background:#eee'>{h}</th>"
                         for h in ("Factura", "Pedido", "Cliente", "Estatus", "Fecha")) + "</tr>"
        f"{filas_html}</table>"
        "<p style='color:#666;font-size:12px'>Puede consultar en cualquier momento en la página de "
        "tracking con el número de factura, pedido o tracking.</p>"
    )
    return Correo(
        para=[vendedor.email],
        asunto=f"Estatus de {titulo} ({len(facturas)}) - {codigo}",
        texto=saludo + "\n\n" + "\n".join(filas_txt),
        html=cuerpo_html,
    )


def correo_cobranzas(destinos: list[str], excel: bytes, nombre: str, remesas: int, facturas: int) -> Correo:
    texto = (
        f"Adjunto el Excel de entregas: {remesas} remesa(s), {facturas} factura(s), con código de vendedor "
        "y estatus final."
    )
    return Correo(
        para=destinos,
        asunto=f"Excel de entregas para Cobranzas - {nombre}",
        texto=texto,
        html=f"<p>{html.escape(texto)}</p>",
        adjuntos=[(nombre, excel, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")],
    )
