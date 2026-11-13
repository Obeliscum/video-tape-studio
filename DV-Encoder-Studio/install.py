#!/usr/bin/env python3
"""Instala un lanzador para el usuario actual sin privilegios de administrador."""
import os
from pathlib import Path
import sys

root = Path(__file__).resolve().parent
applications = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "applications"
applications.mkdir(parents=True, exist_ok=True)
python = Path(sys.executable)
# Desktop Entry usa sus propias reglas de escape para argumentos entre comillas.
def quote(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace('$', '\\$').replace('`', '\\`').replace("%", "%%") + '"'

desktop = applications / "dv-encoder-studio.desktop"
desktop.write_text(
    "[Desktop Entry]\nType=Application\nName=DV Encoder Studio\n"
    "Comment=Codifica MiniDV y crea recuerdos familiares\n"
    f"Exec={quote(python)} {quote(root / 'dv_encoder_studio.py')}\n"
    f"Icon={root / 'dv-encoder-studio.png'}\nStartupWMClass=Dv-encoder-studio\n"
    "Terminal=false\nCategories=AudioVideo;Video;\n", encoding="utf-8",
)
print(f"Lanzador instalado en {desktop}")
