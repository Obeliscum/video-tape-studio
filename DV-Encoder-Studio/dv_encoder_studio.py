#!/usr/bin/python3
# Copyright (c) 2026 Obeliscum. Licensed under the MIT License.
"""Convertidor por lotes de MiniDV a HEVC para Linux."""

from collections import deque
from contextlib import contextmanager
from fractions import Fraction
import os
from pathlib import Path
from queue import Empty, Queue
import selectors
import shutil
import subprocess
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from preferences import load_settings, save_settings, output_path

DEFAULT_ROOT_DIR = str(Path.home() / "Videos")
APP_ID = "dv-encoder-studio"
APP_DIR = Path(__file__).resolve().parent
ENGINE_IDS = ("intel-vaapi", "cpu-fast", "cpu-slow")
DEFAULT_ENGINE = "intel-vaapi"
DEFAULT_QUALITY = 22


def load_encoding_settings():
    settings = load_settings()
    engine = settings.get("engine")
    quality = settings.get("quality")
    return (
        engine if engine in ENGINE_IDS else DEFAULT_ENGINE,
        quality if type(quality) is int and 16 <= quality <= 26 else DEFAULT_QUALITY,
    )


def save_encoding_settings(engine, quality):
    save_settings(engine=engine, quality=quality)


class Tooltip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.pending = None
        self.popup = None
        widget.bind("<Enter>", self.schedule, add="+")
        for event in ("<Leave>", "<ButtonPress>", "<Unmap>", "<Destroy>"):
            widget.bind(event, self.hide, add="+")

    def schedule(self, _event):
        self.hide()
        self.pending = self.widget.after(500, self.show)

    def show(self):
        self.pending = None
        widget = self.widget
        self.popup = popup = tk.Toplevel(widget)
        popup.withdraw()
        popup.overrideredirect(True)
        popup.transient(widget.winfo_toplevel())
        ttk.Label(popup, text=self.text, wraplength=365, justify="left",
                  padding=9, relief="solid").pack()
        popup.update_idletasks()
        x = max(0, min(widget.winfo_rootx(), widget.winfo_screenwidth() - popup.winfo_reqwidth()))
        y = widget.winfo_rooty() + widget.winfo_height() + 6
        if y + popup.winfo_reqheight() > widget.winfo_screenheight():
            y = max(0, widget.winfo_rooty() - popup.winfo_reqheight() - 6)
        popup.geometry(f"+{x}+{y}")
        popup.deiconify()

    def hide(self, _event=None):
        if self.pending is not None:
            self.widget.after_cancel(self.pending)
            self.pending = None
        if self.popup is not None:
            self.popup.destroy()
            self.popup = None


class Cancelled(Exception):
    pass


@contextmanager
def inhibit_sleep():
    """El descriptor mantiene el bloqueo aunque cambie el proceso FFmpeg."""
    try:
        import dbus
        bus = dbus.SystemBus(private=True)
    except Exception as exc:
        raise RuntimeError(f"No se pudo conectar con el gestor de energía: {exc}") from exc
    try:
        try:
            manager = bus.get_object("org.freedesktop.login1", "/org/freedesktop/login1",
                                     introspect=False)
            lock = manager.Inhibit(
                "sleep:idle", "DV-Encoder-Studio", "Codificando cola de vídeos MiniDV",
                "block", dbus_interface="org.freedesktop.login1.Manager", timeout=10,
            )
        except Exception as exc:
            raise RuntimeError(f"No se concedió el bloqueo de suspensión e hibernación: {exc}") from exc
        fd = lock.take()
        try:
            yield
        finally:
            os.close(fd)
    finally:
        bus.close()


def intel_render_nodes():
    for device in sorted(Path("/dev/dri").glob("renderD*")):
        try:
            vendor = (Path("/sys/class/drm") / device.name / "device/vendor").read_text().strip()
        except OSError:
            continue
        if vendor == "0x8086":
            yield str(device)


def media_info(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,codec_name,display_aspect_ratio",
         "-of", "json", path],
        capture_output=True, text=True, errors="replace", timeout=30, check=True,
    )
    data = json.loads(result.stdout)
    video = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    duration = float(data["format"]["duration"])
    if video is None or duration <= 0:
        raise ValueError("No se pudo identificar un vídeo con duración válida.")
    ratio = video.get("display_aspect_ratio")
    aspect = Fraction(ratio.replace(":", "/")) if ratio and ratio != "N/A" else None
    audio_tracks = sum(s["codec_type"] == "audio" for s in data["streams"])
    return duration, aspect, audio_tracks, video["codec_name"]


def validate_output(path, source):
    duration, aspect, audio_tracks, codec = media_info(path)
    source_duration, source_aspect, source_audio_tracks, _ = source
    if (codec != "hevc" or audio_tracks != source_audio_tracks
            or (source_aspect is not None and aspect != source_aspect)
            or abs(duration - source_duration) > 0.15):
        raise ValueError("El MP4 no coincide con la duración, proporción o pistas del original.")


def encoder_command(source, target, engine, quality, deinterlacer, device=None):
    # Mantener el raster y el SAR conserva tanto DV 4:3 como DV 16:9.
    filters = f"{deinterlacer}=mode=send_field:parity=bff:deint=all"
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "warning",
           "-progress", "pipe:1", "-xerror", "-y"]
    if engine == 0:
        cmd += ["-vaapi_device", device]
    cmd += ["-i", source, "-map", "0:v:0", "-map", "0:a?"]
    if engine == 0:
        cmd += ["-vf", filters + ",format=nv12,hwupload", "-c:v", "hevc_vaapi", "-qp", str(quality)]
    else:
        preset = "fast" if engine == 1 else "slow"
        cmd += ["-vf", filters, "-c:v", "libx265", "-preset", preset,
                "-crf", str(quality), "-pix_fmt", "yuv420p"]
    return cmd + ["-tag:v", "hvc1", "-c:a", "aac", "-b:a", "256k",
                  "-ar", "48000", "-movflags", "+faststart", target]


def run_encoder(cmd, cancel, on_progress):
    with tempfile.TemporaryFile() as log, subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=log,
    ) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                buffer = b""
                progress = {}
                while True:
                    if cancel.is_set():
                        raise Cancelled
                    if not selector.select(timeout=0.2):
                        continue
                    chunk = os.read(process.stdout.fileno(), 8192)
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        key, sep, value = line.decode(errors="replace").strip().partition("=")
                        if sep:
                            progress[key] = value
                        if key == "progress":
                            on_progress(progress)
                            progress = {}
            while process.poll() is None:
                if cancel.wait(0.1):
                    raise Cancelled
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        if process.returncode:
            log.seek(max(0, log.seek(0, os.SEEK_END) - 8192))
            tail = deque(log.read().decode(errors="replace").splitlines(), maxlen=7)
            raise RuntimeError("\n".join(tail) or f"FFmpeg terminó con código {process.returncode}.")


class EncoderStudioApp(tk.Tk):
    def __init__(self):
        super().__init__(className=APP_ID)
        self.title("DV Encoder Studio · MiniDV a H.265")
        self.geometry("1140x840")
        self.minsize(960, 700)
        self.icon = tk.PhotoImage(file=str(APP_DIR / "dv-encoder-studio.png"))
        self.iconphoto(True, self.icon)
        self.file_items = {}
        self.events = Queue()
        self.cancel = threading.Event()
        self.is_encoding = False
        self.is_scanning = False
        self.close_when_done = False
        self.path_preferences = load_settings()
        self.create_widgets()
        self.protocol("WM_DELETE_WINDOW", self.close_app)
        self.after(100, self.process_events)
        self.refresh_file_list()

    def create_widgets(self):
        # 1. Selector de Carpeta Raíz
        top_frame = ttk.LabelFrame(self, text=" Directorio de Trabajo (NFS / Local) ", padding=10)
        top_frame.pack(fill="x", padx=12, pady=6)

        f_in = ttk.Frame(top_frame)
        f_in.pack(fill="x", pady=2)
        ttk.Label(f_in, text="Carpeta de Cintas DV:", width=22, anchor="w").pack(side="left")
        self.entry_root = ttk.Entry(f_in)
        self.entry_root.insert(0, self.path_preferences.get("source_dir", DEFAULT_ROOT_DIR))
        self.entry_root.pack(side="left", expand=True, fill="x", padx=5)
        ttk.Button(f_in, text="Examinar...", command=self.browse_root_dir).pack(side="left")

        destination = ttk.Frame(top_frame)
        destination.pack(fill="x", pady=2)
        ttk.Label(destination, text="Destino de los MP4:", width=22, anchor="w").pack(side="left")
        self.entry_destination = ttk.Entry(destination)
        self.entry_destination.insert(0, self.path_preferences.get("encoded_dir", ""))
        self.entry_destination.pack(side="left", expand=True, fill="x", padx=5)
        ttk.Button(destination, text="Examinar…", command=self.browse_destination).pack(side="left")
        ttk.Button(destination, text="Por cinta", command=self.clear_destination).pack(side="left", padx=4)
        Tooltip(self.entry_destination,
                "Elige una carpeta de salida. Se conservan las subcarpetas de las cintas para evitar "
                "mezclar nombres iguales. Vacío guarda en codificados dentro de cada cinta.")
        ttk.Label(top_frame, text="Tras cambiar las rutas, pulsa Refrescar lista.",
                  foreground="#2563eb").pack(anchor="w", pady=(4, 0))

        # 2. Parámetros de Render
        opt_frame = ttk.LabelFrame(self, text=" Configuración del Motor de Render ", padding=10)
        opt_frame.pack(fill="x", padx=12, pady=6)

        opt_grid = ttk.Frame(opt_frame)
        opt_grid.pack(fill="x")

        engine, quality = load_encoding_settings()
        engine_label = ttk.Label(opt_grid, text="Motor:", font=("Sans", 9, "bold"))
        engine_label.grid(row=0, column=0, padx=5, sticky="w")
        self.combo_engine = ttk.Combobox(
            opt_grid,
            values=[
                "Intel VA-API (HEVC)",
                "CPU libx265 (Preset Fast - Rápido y Robusto)",
                "CPU libx265 (Preset Slow - Mayor Compresión)"
            ],
            width=38,
            state="readonly"
        )
        self.combo_engine.current(ENGINE_IDS.index(engine))
        self.combo_engine.grid(row=0, column=1, padx=5, sticky="w")

        quality_label = ttk.Label(opt_grid, text="Calidad (CRF/QP):", font=("Sans", 9, "bold"))
        quality_label.grid(row=0, column=2, padx=(15, 5), sticky="w")
        self.spin_qp = ttk.Spinbox(opt_grid, from_=16, to=26, width=5)
        self.spin_qp.set(str(quality))
        self.spin_qp.grid(row=0, column=3, padx=5, sticky="w")
        engine_help = (
            "Intel VA-API usa la GPU Intel para comprimir el vídeo y requiere un dispositivo compatible. "
            "CPU Fast tarda menos que CPU Slow. CPU Slow dedica más tiempo a mejorar la compresión."
        )
        quality_help = (
            "De 16 a 26. Un número menor suele conservar más detalle y generar un MP4 más grande. "
            "Intel usa QP y la CPU usa CRF. El mismo número puede dar resultados distintos con cada motor."
        )
        for widget in (engine_label, self.combo_engine):
            Tooltip(widget, engine_help)
        for widget in (quality_label, self.spin_qp):
            Tooltip(widget, quality_help)

        ttk.Label(opt_grid, text="Desentrelazado:", font=("Sans", 9, "bold")).grid(row=0, column=4, padx=(15, 5), sticky="w")
        self.combo_deint = ttk.Combobox(opt_grid, values=["bwdif (doble frecuencia)", "yadif (doble frecuencia)"], width=25, state="readonly")
        self.combo_deint.current(0)
        self.combo_deint.grid(row=0, column=5, padx=5, sticky="w")

        # 3. Monitor de Progreso Dual
        prog_frame = ttk.LabelFrame(self, text=" Progreso de Renderizado ", padding=10)
        prog_frame.pack(fill="x", padx=12, pady=6)

        p_glob_top = ttk.Frame(prog_frame)
        p_glob_top.pack(fill="x")
        self.lbl_global_title = ttk.Label(p_glob_top, text="Cola completa: 0 de 0 completados", font=("Sans", 9, "bold"))
        self.lbl_global_title.pack(side="left")
        self.lbl_global_eta = ttk.Label(p_glob_top, text="Tiempo restante estimado: --:--:--", font=("Monospace", 9), foreground="#2563eb")
        self.lbl_global_eta.pack(side="right")

        self.prog_global = ttk.Progressbar(prog_frame, maximum=100, value=0)
        self.prog_global.pack(fill="x", pady=(2, 8))

        p_file_top = ttk.Frame(prog_frame)
        p_file_top.pack(fill="x")
        self.lbl_file_title = ttk.Label(p_file_top, text="Archivo actual: En espera", font=("Monospace", 9))
        self.lbl_file_title.pack(side="left")
        self.lbl_file_stats = ttk.Label(p_file_top, text="0.0% | 0 fps | 0.0x", font=("Monospace", 9), foreground="#16a34a")
        self.lbl_file_stats.pack(side="right")

        self.prog_file = ttk.Progressbar(prog_frame, maximum=100, value=0)
        self.prog_file.pack(fill="x", pady=(2, 2))

        # 4. Tabla de Archivos
        list_frame = ttk.LabelFrame(self, text=" Archivos de Vídeo Detectados ", padding=10)
        list_frame.pack(expand=True, fill="both", padx=12, pady=6)

        columns = ("name", "tape_folder", "raw_size", "mp4_exists", "mp4_size", "ratio")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="browse")

        self.tree.heading("name", text="Archivo Bruto (.dv)")
        self.tree.heading("tape_folder", text="Cinta / Carpeta")
        self.tree.heading("raw_size", text="Tamaño DV")
        self.tree.heading("mp4_exists", text="Estado H.265")
        self.tree.heading("mp4_size", text="Tamaño MP4")
        self.tree.heading("ratio", text="Reducción")

        self.tree.column("name", width=280)
        self.tree.column("tape_folder", width=200)
        self.tree.column("raw_size", width=100, anchor="center")
        self.tree.column("mp4_exists", width=110, anchor="center")
        self.tree.column("mp4_size", width=100, anchor="center")
        self.tree.column("ratio", width=90, anchor="center")

        tree_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side="left", expand=True, fill="both")
        tree_scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda event: self.preview_mp4())

        # 5. Botonera
        act_frame = ttk.Frame(self, padding=6)
        act_frame.pack(fill="x", padx=12)

        ttk.Button(act_frame, text="🔄 Refrescar Lista", command=self.refresh_file_list).pack(side="left", padx=3)
        ttk.Button(act_frame, text="▶ Codificar Selección", command=self.encode_selected).pack(side="left", padx=3)
        queue_button = ttk.Button(act_frame, text="⏩ Codificar Todo en Cola", command=self.encode_all)
        queue_button.pack(side="left", padx=3)
        Tooltip(queue_button,
                "Codifica los pendientes y comprueba los MP4 existentes. Salta los válidos y vuelve a "
                "codificar los que fallan la comprobación. Si todos tienen MP4, te pregunta si quieres "
                "recodificarlos todos. Si respondes No, la cola no empieza.")
        self.btn_cancel = ttk.Button(act_frame, text="⏹ Detener", command=self.cancel_encoding, state="disabled")
        self.btn_cancel.pack(side="left", padx=3)

        ttk.Button(act_frame, text="🎬 Crear recuerdo…", command=self.open_recuerdo).pack(side="right", padx=3)

        ttk.Separator(act_frame, orient="vertical").pack(side="left", fill="y", padx=8)

        ttk.Button(act_frame, text="👁 Ver MP4 (H.265)", command=self.preview_mp4).pack(side="left", padx=3)
        ttk.Button(act_frame, text="👁 Ver Original (.dv)", command=self.preview_dv).pack(side="left", padx=3)

        # 6. Consola de FFmpeg
        log_frame = ttk.LabelFrame(self, text=" Registro de Telemetría FFmpeg ", padding=8)
        log_frame.pack(fill="x", padx=12, pady=(4, 10))

        log_toolbar = ttk.Frame(log_frame)
        log_toolbar.pack(fill="x", pady=(0, 4))
        ttk.Button(log_toolbar, text="📋 Copiar Registro", command=self.copy_log_to_clipboard).pack(side="left", padx=2)
        ttk.Button(log_toolbar, text="🧹 Limpiar Registro", command=self.clear_log).pack(side="left", padx=2)

        text_container = ttk.Frame(log_frame)
        text_container.pack(fill="both", expand=True)
        self.txt_log = tk.Text(text_container, height=5, bg="#121212", fg="#00ff66", font=("Monospace", 9), wrap="word")
        self.txt_log.pack(side="left", expand=True, fill="both")
        log_scroll = ttk.Scrollbar(text_container, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")

    def open_recuerdo(self):
        if self.is_encoding:
            messagebox.showinfo("Codificación en curso", "Espera a que termine la cola para crear un recuerdo.")
            return
        from recuerdo import RecuerdoWindow
        RecuerdoWindow(self, inhibit_sleep, run_encoder, Cancelled)

    def human_size(self, num_bytes):
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if abs(num_bytes) < 1024.0:
                return f"{num_bytes:3.1f} {unit}"
            num_bytes /= 1024.0
        return f"{num_bytes:.1f} PB"

    def format_time(self, seconds):
        if seconds <= 0 or seconds > 86400 * 3:
            return "--:--:--"
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    def browse_root_dir(self):
        folder = filedialog.askdirectory(initialdir=self.entry_root.get())
        if folder:
            self.entry_root.delete(0, "end")
            self.entry_root.insert(0, folder)
            self.refresh_file_list()

    def browse_destination(self):
        folder = filedialog.askdirectory(initialdir=self.entry_destination.get() or self.entry_root.get())
        if folder:
            self.entry_destination.delete(0, "end")
            self.entry_destination.insert(0, folder)
            self.refresh_file_list()

    def clear_destination(self):
        self.entry_destination.delete(0, "end")
        self.refresh_file_list()

    def copy_log_to_clipboard(self):
        text = self.txt_log.get("1.0", "end-1c").strip()
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
            messagebox.showinfo("Copiado", "Registro copiado al portapapeles.")

    def clear_log(self):
        self.txt_log.delete("1.0", "end")

    def log_msg(self, msg):
        self.txt_log.insert("end", msg + "\n")
        if int(self.txt_log.index("end-1c").split(".")[0]) > 2000:
            self.txt_log.delete("1.0", "100.0")
        self.txt_log.see("end")

    def refresh_file_list(self):
        if self.is_encoding or self.is_scanning:
            return
        source = self.entry_root.get().strip()
        if not source or not Path(source).expanduser().is_dir():
            self.log_msg("Selecciona una carpeta de origen existente.")
            return
        root = Path(source).expanduser().resolve()
        value = self.entry_destination.get().strip()
        destination = Path(value).expanduser().resolve() if value else None
        if destination == root:
            self.log_msg("Elige un destino distinto de la carpeta de origen.")
            return
        self.path_preferences.update(source_dir=str(root), encoded_dir=str(destination) if destination else "")
        try:
            save_settings(source_dir=str(root), encoded_dir=str(destination) if destination else "")
        except OSError as exc:
            self.log_msg(f"No se pudieron guardar las rutas: {exc}")
        self.is_scanning = True
        self.lbl_global_title.config(text="Buscando archivos DV…")
        threading.Thread(target=self.scan_folder, args=(root, destination), daemon=True).start()

    def scan_folder(self, root, destination=None):
        items = {}

        def log_error(exc):
            self.events.put(("log", f"No se pudo leer: {exc}"))

        try:
            for folder, dirs, files in os.walk(root, onerror=log_error):
                dirs[:] = sorted(d for d in dirs if d.lower() != "codificados"
                                 and (destination is None or (Path(folder) / d).resolve() != destination))
                for name in sorted(files):
                    if not name.lower().endswith(".dv"):
                        continue
                    dv = Path(folder) / name
                    mp4 = output_path(dv, root, destination)
                    try:
                        size = dv.stat().st_size
                        mp4_size = self.output_size(mp4)
                    except OSError as exc:
                        log_error(exc)
                        continue
                    items[str(dv)] = {
                        "dv_path": dv, "mp4_path": mp4, "fname": name,
                        "tape_folder": str(dv.parent.relative_to(root)) if dv.parent != root else "(Raíz)",
                        "size": size, "mp4_size": mp4_size,
                    }
        except Exception as exc:
            log_error(exc)
        finally:
            self.events.put(("scan", items))

    @staticmethod
    def output_size(path):
        try:
            return path.stat().st_size
        except FileNotFoundError:
            return 0

    def update_row(self, item, status=None):
        dv = str(item["dv_path"])
        mp4_size = item["mp4_size"]
        size = item["size"]
        ratio = f"{(size - mp4_size) / size:+.0%}" if mp4_size and size else "—"
        values = (item["fname"], item["tape_folder"], self.human_size(size),
                  status or ("MP4 existente" if mp4_size else "Pendiente"),
                  self.human_size(mp4_size) if mp4_size else "—", ratio)
        if self.tree.exists(dv):
            self.tree.item(dv, values=values)
        else:
            self.tree.insert("", "end", iid=dv, values=values)

    def encode_selected(self):
        if self.is_encoding or self.is_scanning:
            return
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("Selecciona un vídeo", "Selecciona un archivo DV de la lista.")
            return
        item = self.file_items[selected[0]]
        if item["mp4_size"] and not messagebox.askyesno(
            "Recodificar", f"¿Volver a codificar {item['fname']}?",
        ):
            return
        self.start_encoding_queue([item], replace_existing=bool(item["mp4_size"]))

    def encode_all(self):
        if self.is_encoding or self.is_scanning:
            return
        items = list(self.file_items.values())
        if not items:
            messagebox.showinfo("Sin archivos", "No hay archivos DV detectados.")
            return
        replace_existing = all(item["mp4_size"] for item in items)
        if replace_existing and not messagebox.askyesno(
            "Recodificar", "Todos tienen un MP4. ¿Quieres recodificarlos todos?",
        ):
            return
        self.start_encoding_queue(items, replace_existing=replace_existing)

    def start_encoding_queue(self, items, replace_existing):
        try:
            quality = int(self.spin_qp.get())
            if not 16 <= quality <= 26:
                raise ValueError
        except ValueError:
            messagebox.showerror("Calidad", "Introduce un número entero entre 16 y 26.")
            return
        for command in ("ffmpeg", "ffprobe"):
            if not shutil.which(command):
                messagebox.showerror("Falta un programa", f"No se encuentra {command}.")
                return
        engine = self.combo_engine.current()
        try:
            save_encoding_settings(ENGINE_IDS[engine], quality)
            self.path_preferences.update(engine=ENGINE_IDS[engine], quality=quality)
        except OSError as exc:
            self.log_msg(f"No se pudieron guardar el motor y la calidad: {exc}")
        deinterlacer = "bwdif" if self.combo_deint.current() == 0 else "yadif"
        self.cancel.clear()
        self.is_encoding = True
        self.btn_cancel.config(state="normal")
        self.lbl_global_title.config(text=f"Cola: 0 de {len(items)} revisados")
        self.lbl_global_eta.config(text="Solicitando bloqueo de suspensión…")
        self.prog_global.config(value=0)
        self.prog_file.config(value=0)
        threading.Thread(target=self.encode_queue,
                         args=(items, replace_existing, engine, quality, deinterlacer)).start()

    def encode_queue(self, items, replace_existing, engine, quality, deinterlacer):
        counts = {"correctos": 0, "ya listos": 0, "fallidos": 0}
        stopped = False
        error = None
        try:
            with inhibit_sleep():
                self.events.put(("log", "Suspensión e hibernación bloqueadas para toda la cola."))
                self.events.put(("eta", "Bloqueo de suspensión activo"))
                device = next((d for d in intel_render_nodes() if os.access(d, os.R_OK | os.W_OK)), None)
                if engine == 0 and device is None:
                    raise RuntimeError("No se encontró un dispositivo Intel VA-API accesible. Selecciona libx265.")
                start = time.monotonic()
                total = sum(max(1, item["size"]) for item in items)
                done = 0
                for index, item in enumerate(items, 1):
                    if self.cancel.is_set():
                        raise Cancelled
                    self.events.put(("current", (index, len(items), item["fname"])))
                    dv, target = item["dv_path"], item["mp4_path"]
                    weight = max(1, item["size"])

                    def progress(values):
                        try:
                            seconds = int(values.get("out_time_us", "0")) / 1_000_000
                        except ValueError:
                            return
                        fraction = max(0.0, min(0.99, seconds / source[0]))
                        processed = done + fraction * weight
                        elapsed = time.monotonic() - start
                        eta = elapsed * (total - processed) / processed if processed else 0
                        stats = f"{fraction:.1%} | {values.get('fps', '—')} fps | {values.get('speed', '—')}"
                        self.events.put(("progress", (fraction * 100, processed / total * 100, stats, eta)))

                    try:
                        source = media_info(dv)
                        if self.cancel.is_set():
                            raise Cancelled
                        if not replace_existing and self.output_size(target):
                            try:
                                validate_output(target, source)
                            except (ValueError, KeyError, subprocess.CalledProcessError):
                                pass
                            else:
                                counts["ya listos"] += 1
                                self.events.put(("row", (str(dv), target.stat().st_size, "Verificado")))
                                continue
                        size = self.encode_clip(dv, target, source, engine, quality, deinterlacer, device, progress)
                        counts["correctos"] += 1
                        self.events.put(("row", (str(dv), size, "Verificado")))
                        self.events.put(("log", f"✔ {item['fname']}"))
                    except Cancelled:
                        raise
                    except Exception as exc:
                        counts["fallidos"] += 1
                        self.events.put(("row", (str(dv), None, "Error")))
                        detail = getattr(exc, "stderr", None) or str(exc)
                        self.events.put(("log", f"✖ {item['fname']}: {detail.strip()}"))
                    finally:
                        if not self.cancel.is_set():
                            done += weight
                            self.events.put(("reviewed", (index, len(items), done / total * 100)))
        except Cancelled:
            stopped = True
        except Exception as exc:
            error = str(exc)
        finally:
            self.events.put(("finished", (counts, stopped or self.cancel.is_set(), error)))

    def encode_clip(self, dv, target, source, engine, quality, deinterlacer, device, progress):
        target.parent.mkdir(parents=True, exist_ok=True)
        # Mismo directorio: el reemplazo se realiza en una sola operación.
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".dvstudio-",
                                         suffix=".mp4", delete=False) as handle:
            temporary = Path(handle.name)
        try:
            if self.cancel.is_set():
                raise Cancelled
            command = encoder_command(dv, temporary, engine, quality, deinterlacer, device)
            run_encoder(command, self.cancel, progress)
            validate_output(temporary, source)
            if self.cancel.is_set():
                raise Cancelled
            size = temporary.stat().st_size
            os.replace(temporary, target)
            return size
        finally:
            temporary.unlink(missing_ok=True)

    def process_events(self):
        for _ in range(100):
            try:
                kind, value = self.events.get_nowait()
            except Empty:
                break
            if kind == "log":
                self.log_msg(value)
            elif kind == "scan":
                self.is_scanning = False
                self.file_items = value
                self.tree.delete(*self.tree.get_children())
                for item in value.values():
                    self.update_row(item)
                existing = sum(bool(item["mp4_size"]) for item in value.values())
                self.lbl_global_title.config(text=f"{len(value)} DV detectados · {existing} con MP4")
                self.prog_global.config(value=0)
            elif kind == "eta":
                self.lbl_global_eta.config(text=value)
            elif kind == "current":
                index, total, name = value
                self.lbl_file_title.config(text=f"Procesando [{index}/{total}]: {name}")
                self.lbl_file_stats.config(text="")
                self.prog_file.config(value=0)
            elif kind == "progress" and not self.cancel.is_set():
                clip, total, stats, eta = value
                self.prog_file.config(value=clip)
                self.prog_global.config(value=total)
                self.lbl_file_stats.config(text=stats)
                self.lbl_global_eta.config(text=f"Tiempo restante: {self.format_time(eta)} · Bloqueo activo")
            elif kind == "row":
                dv, size, status = value
                item = self.file_items[dv]
                if size is not None:
                    item["mp4_size"] = size
                self.update_row(item, status)
            elif kind == "reviewed":
                index, total, percent = value
                self.lbl_global_title.config(text=f"Cola: {index} de {total} revisados")
                self.prog_global.config(value=percent)
            elif kind == "finished":
                self.finish_queue(*value)
                if self.close_when_done:
                    self.destroy()
                    return
        self.after(100, self.process_events)

    def finish_queue(self, counts, stopped, error):
        self.is_encoding = False
        self.btn_cancel.config(state="disabled")
        status = "Cola interrumpida" if error else "Cola cancelada" if stopped else (
            "Cola finalizada con errores" if counts["fallidos"] else "Cola finalizada"
        )
        self.lbl_file_title.config(text=status)
        self.lbl_file_stats.config(text="")
        self.lbl_global_eta.config(text="")
        self.prog_file.config(value=0)
        summary = " · ".join(f"{count} {name}" for name, count in counts.items())
        self.lbl_global_title.config(text=summary)
        self.log_msg(f"{status}. {summary}")
        if error:
            self.log_msg(error)
            if not self.close_when_done:
                messagebox.showerror(status, error)

    def cancel_encoding(self):
        if self.is_encoding and not self.cancel.is_set() and messagebox.askyesno(
            "Detener", "¿Deseas detener la cola de codificación?",
        ):
            self.cancel.set()
            self.btn_cancel.config(state="disabled")
            self.lbl_global_eta.config(text="Deteniendo la cola…")

    def close_app(self):
        if not self.is_encoding:
            self.destroy()
            return
        if self.cancel.is_set() or messagebox.askyesno(
            "Cerrar", "Hay una cola en curso. ¿Detenerla y cerrar?",
        ):
            self.close_when_done = True
            self.cancel.set()
            self.btn_cancel.config(state="disabled")

    def run_player(self, file_path):
        if not os.path.exists(file_path):
            messagebox.showerror("Error", f"El archivo no existe:\n{file_path}")
            return
        player = None
        for p in ["mpv", "vlc", "ffplay", "xdg-open"]:
            if shutil.which(p):
                player = p
                break
        if player:
            try:
                subprocess.Popen([player, file_path])
            except OSError as exc:
                messagebox.showerror("Error al abrir el reproductor", str(exc))
        else:
            messagebox.showerror("Sin reproductor", "No se ha encontrado mpv, vlc ni ffplay.")

    def preview_mp4(self):
        sel = self.tree.selection()
        if not sel:
            return
        item = self.file_items.get(sel[0])
        if item and os.path.exists(item["mp4_path"]):
            self.run_player(item["mp4_path"])
        else:
            messagebox.showinfo("No codificado", "El archivo H.265 aún no existe. Codifícalo primero.")

    def preview_dv(self):
        sel = self.tree.selection()
        if not sel:
            return
        item = self.file_items.get(sel[0])
        if item and os.path.exists(item["dv_path"]):
            self.run_player(item["dv_path"])


if __name__ == "__main__":
    EncoderStudioApp().mainloop()
