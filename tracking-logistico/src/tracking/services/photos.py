"""Almacenamiento de fotos de entrega/devolución en disco (escritura atómica, idempotente por UUID).

Para la nube basta con montar un volumen persistente (o sincronizar `STORAGE_DIR` a un bucket).
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from tracking.services.timeutil import now_utc

_UUID_RE = re.compile(r"^[0-9a-fA-F-]{8,36}$")
_MAGIC: dict[str, tuple[bytes, ...]] = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/webp": (b"RIFF",),
}
_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


class FotoInvalida(ValueError):
    pass


def detectar_tipo(data: bytes) -> str:
    for ctype, firmas in _MAGIC.items():
        if any(data.startswith(f) for f in firmas):
            if ctype == "image/webp" and data[8:12] != b"WEBP":
                continue
            return ctype
    raise FotoInvalida("El archivo no es una imagen JPEG, PNG o WEBP válida")


def validar_id(foto_id: str) -> str:
    if not _UUID_RE.match(foto_id):
        raise FotoInvalida("Identificador de foto inválido")
    return foto_id.lower()


class PhotoStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def guardar(self, foto_id: str, factura_id: int | str, data: bytes) -> tuple[str, str, str]:
        """Devuelve (ruta_relativa, content_type, sha256). ``factura_id`` es la subcarpeta (id o nombre)."""
        ctype = detectar_tipo(data)
        sha = hashlib.sha256(data).hexdigest()
        hoy = now_utc()
        rel = Path(f"{hoy:%Y}") / f"{hoy:%m}" / str(factura_id) / f"{validar_id(foto_id)}.{_EXT[ctype]}"
        dest = self.root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, dest)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return rel.as_posix(), ctype, sha

    def ruta_absoluta(self, rel: str) -> Path:
        path = (self.root / rel).resolve()
        if self.root not in path.parents:
            raise FotoInvalida("Ruta fuera del almacenamiento")
        return path
