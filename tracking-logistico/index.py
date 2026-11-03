"""Punto de entrada para Vercel: expone la aplicación FastAPI como `app`.

Vercel instala requirements.txt, detecta esta instancia y la publica como una sola función.
La configuración se toma de las variables de entorno del proyecto (ver DESPLIEGUE_VERCEL.md).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from fastapi import FastAPI

from tracking.main import create_app

app: FastAPI = create_app()
