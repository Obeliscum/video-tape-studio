# Copyright (c) 2026 Obeliscum. Licensed under the MIT License.
"""Preferencias locales del usuario, fuera del directorio de instalación."""
import json
import os
from pathlib import Path
import tempfile

SETTINGS_FILE = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "dv-encoder-studio" / "settings.json"


def load_settings():
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(**changes):
    data = load_settings()
    data.update(changes)
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=SETTINGS_FILE.parent, mode="w", encoding="utf-8",
                                     prefix=".settings-", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.close()
            temporary.replace(SETTINGS_FILE)
        finally:
            temporary.unlink(missing_ok=True)


def output_path(source, root, destination):
    """Un destino vacío conserva el esquema histórico de carpetas por cinta."""
    folder = destination / source.parent.relative_to(root) if destination else source.parent / "codificados"
    return folder / (source.stem + ".mp4")
