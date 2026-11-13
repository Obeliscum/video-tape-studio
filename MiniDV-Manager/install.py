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

desktop = applications / "minidv-manager.desktop"
desktop.write_text(
    "[Desktop Entry]\nType=Application\nName=MiniDV Manager\n"
    "Comment=Captura cintas por FireWire y organiza el catálogo\n"
    f"Exec={quote(python)} {quote(root / 'minidv_manager.py')}\n"
    f"Icon={root / 'video-tape.png'}\nStartupWMClass=Minidv-manager\n"
    "Terminal=false\nCategories=AudioVideo;Video;\n", encoding="utf-8",
)
print(f"Lanzador instalado en {desktop}")
