#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 Obeliscum. Licensed under the MIT License.
"""
MiniDV Catalog & Capture Manager - Capture, Catalog & VCR
Control remoto AV/C, aislamiento local anti-underrun, previsualización en vivo (ffplay),
organización jerárquica por subcarpetas (CÓDIGO_TÍTULO) y barra de progreso integrada en consola.
"""

import os
import json
import tempfile
from pathlib import Path
import re
import sys
import glob
import time
import sqlite3
import subprocess
import threading
import shutil
import signal
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

APP_NAME = "minidv-manager"
CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP_NAME / "settings.json"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / APP_NAME
LEGACY_DB = Path.home() / "minidv_catalog.db"
DEFAULT_DB = LEGACY_DB if LEGACY_DB.is_file() else DATA_DIR / "catalog.db"
LOCAL_CAPTURE_DIR = str(Path.home() / "Videos/MiniDV/Capturas")
DEFAULT_ARCHIVE_DIR = str(Path.home() / "Videos/MiniDV/Archivo")
SPLIT_MODES = {
    "Por tomas originales": ["--autosplit"],
    "Continuo (cinta completa)": [],
    "Pausas de 60 s en fecha/hora": ["--autosplit=60"],
}


def load_settings():
    try:
        settings = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return settings if isinstance(settings, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(settings):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=CONFIG_FILE.parent, mode="w", encoding="utf-8",
                                     prefix=".settings-", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(settings, handle, ensure_ascii=False, indent=2)
            handle.close()
            temporary.replace(CONFIG_FILE)
        finally:
            temporary.unlink(missing_ok=True)


def capture_command(prefix, mode):
    return ["dvgrab", "--format", "raw", "--size", "0", "--frames", "0",
            *SPLIT_MODES[mode], "--timestamp", "--nostop", prefix]


def sanitize_folder_name(name):
    """Limpia el texto para generar nombres de directorio seguros en Linux/NFS."""
    clean = re.sub(r'[^\w\-\.]', '_', name.strip())
    clean = re.sub(r'_+', '_', clean)
    return clean.strip('_')

class MiniDVApp(tk.Tk):
    def __init__(self):
        super().__init__(className="minidv-manager")
        icon_path = Path(__file__).resolve().parent / "video-tape.png"
        if icon_path.is_file():
            self.icon = tk.PhotoImage(file=str(icon_path))
            self.iconphoto(True, self.icon)
        self.title("MiniDV Manager · Captura y catálogo de cintas")
        self.geometry("1100x840")
        self.minsize(940, 700)

        self.capture_process = None
        self.is_capturing = False
        self.is_transferring = False
        self.is_converting = False
        self.closing = False
        self.captured_files_before = set()
        self.led_state = False
        self.last_detected_date = ""

        self.settings = load_settings()
        self.db_file = str(Path(self.settings.get("database", str(DEFAULT_DB))).expanduser())
        self.init_database()
        self.create_menu()
        self.create_widgets()
        self.load_tapes()

    def init_database(self):
        Path(self.db_file).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tapes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tape_code TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                recorded_date TEXT,
                tags TEXT,
                notes TEXT,
                status TEXT DEFAULT 'Pendiente'
            )
        """)
        conn.commit()
        conn.close()

    def create_menu(self):
        menubar = tk.Menu(self)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Verificar Enlace FireWire", command=self.check_firewire)
        file_menu.add_separator()
        file_menu.add_command(label="Salir", command=self.on_closing)
        menubar.add_cascade(label="Archivo", menu=file_menu)

        db_menu = tk.Menu(menubar, tearoff=0)
        db_menu.add_command(label="Refrescar catálogo", command=self.load_tapes)
        menubar.add_cascade(label="Catálogo", menu=db_menu)
        self.config(menu=menubar)

    def create_widgets(self):
        tab_control = ttk.Notebook(self)
        self.tab_capture = ttk.Frame(tab_control)
        self.tab_catalog = ttk.Frame(tab_control)
        self.tab_convert = ttk.Frame(tab_control)
        self.tab_zfs = ttk.Frame(tab_control)

        tab_control.add(self.tab_capture, text=" 📹 Captura, VCR y Previsualización ")
        tab_control.add(self.tab_catalog, text=" 📁 Catálogo de Cintas (BBDD) ")
        tab_control.add(self.tab_convert, text=" ⚙️ Conversión FFmpeg ")
        tab_control.add(self.tab_zfs, text=" 🖧 Destino de archivo ")
        tab_control.pack(expand=1, fill="both")

        self.setup_capture_tab()
        self.setup_catalog_tab()
        self.setup_convert_tab()
        self.setup_zfs_tab()

    # ------------------ PESTAÑA CAPTURA Y VCR ------------------
    def setup_capture_tab(self):
        # 1. Configuración de Destino y Nombre
        cfg_frame = ttk.LabelFrame(self.tab_capture, text=" Destino y Código de Cinta ", padding=10)
        cfg_frame.pack(fill="x", padx=12, pady=6)

        r1 = ttk.Frame(cfg_frame)
        r1.pack(fill="x", pady=2)
        ttk.Label(r1, text="Carpeta temporal local:", width=22, anchor="w").pack(side="left")
        self.entry_capture_dir = ttk.Entry(r1)
        self.entry_capture_dir.insert(0, self.settings.get("capture_dir", LOCAL_CAPTURE_DIR))
        self.entry_capture_dir.pack(side="left", expand=True, fill="x", padx=5)
        ttk.Button(r1, text="Examinar...", command=lambda: self.browse_folder(self.entry_capture_dir)).pack(side="right")

        r2 = ttk.Frame(cfg_frame)
        r2.pack(fill="x", pady=2)
        ttk.Label(r2, text="Código / ID Cinta:", width=22, anchor="w").pack(side="left")
        self.entry_prefix = ttk.Entry(r2, width=20)
        self.entry_prefix.insert(0, "CINTA01")
        self.entry_prefix.pack(side="left", padx=5)

        ttk.Label(r2, text="Título breve:").pack(side="left", padx=(10, 5))
        self.entry_quick_title = ttk.Entry(r2, width=28)
        self.entry_quick_title.insert(0, "Cinta familiar 2002")
        self.entry_quick_title.pack(side="left", padx=5)

        # Casilla de previsualización en vivo
        r3 = ttk.Frame(cfg_frame)
        r3.pack(fill="x", pady=(6, 0))
        self.var_preview = tk.BooleanVar(value=self.settings.get("preview", True))
        chk_preview = ttk.Checkbutton(
            r3,
            text="👁 Activar ventana de previsualización en vivo (ffplay a baja latencia)",
            variable=self.var_preview
        )
        chk_preview.pack(side="left", padx=5)

        split_row = ttk.Frame(cfg_frame)
        split_row.pack(fill="x", pady=(6, 0))
        ttk.Label(split_row, text="Dividir captura:", width=22).pack(side="left")
        mode = self.settings.get("split_mode", "Por tomas originales")
        self.split_mode = tk.StringVar(value=mode if mode in SPLIT_MODES else "Por tomas originales")
        ttk.Combobox(split_row, textvariable=self.split_mode, values=tuple(SPLIT_MODES),
                     state="readonly", width=30).pack(side="left", padx=5)
        ttk.Label(cfg_frame, text="Por tomas puede producir clips de pocos fotogramas. "
                  "Continuo conserva la cinta en un archivo mientras dure la captura.",
                  wraplength=850).pack(anchor="w", pady=(4, 0))

        # 2. Control Remoto VCR
        vcr_frame = ttk.LabelFrame(self.tab_capture, text=" Control Remoto de Videocámara (Transporte FireWire) ", padding=10)
        vcr_frame.pack(fill="x", padx=12, pady=6)

        vcr_bar = ttk.Frame(vcr_frame)
        vcr_bar.pack(fill="x")

        ttk.Button(vcr_bar, text="⏪ Rebobinar (REW)", command=lambda: self.send_vcr_command("rewind")).pack(side="left", padx=3)
        ttk.Button(vcr_bar, text="◀◀ Retroceso (x2)", command=lambda: self.send_vcr_command("reverse")).pack(side="left", padx=3)
        ttk.Button(vcr_bar, text="⏹ Stop VCR", command=lambda: self.send_vcr_command("stop")).pack(side="left", padx=3)
        ttk.Button(vcr_bar, text="⏸ Pausa", command=lambda: self.send_vcr_command("pause")).pack(side="left", padx=3)
        ttk.Button(vcr_bar, text="▶ Play", command=lambda: self.send_vcr_command("play")).pack(side="left", padx=3)
        ttk.Button(vcr_bar, text="▶▶ Avance (x2)", command=lambda: self.send_vcr_command("trickplay 2")).pack(side="left", padx=3)
        ttk.Button(vcr_bar, text="⏩ Adelantar (FF)", command=lambda: self.send_vcr_command("fastforward")).pack(side="left", padx=3)

        # 3. Telemetría de Captura
        dash_frame = ttk.LabelFrame(self.tab_capture, text=" Monitor de Grabación y Telemetría ", padding=10)
        dash_frame.pack(fill="x", padx=12, pady=6)

        top_dash = ttk.Frame(dash_frame)
        top_dash.pack(fill="x", pady=2)
        self.lbl_status_led = tk.Label(top_dash, text="○ EN ESPERA", font=("Sans", 11, "bold"), fg="#777777")
        self.lbl_status_led.pack(side="left", padx=5)
        self.lbl_active_file = tk.Label(top_dash, text="Archivo actual: Ninguno", font=("Monospace", 10), fg="#333333")
        self.lbl_active_file.pack(side="left", padx=15)

        metrics_frame = ttk.Frame(dash_frame)
        metrics_frame.pack(fill="x", pady=8)

        b_tc = tk.Frame(metrics_frame, bg="#181818", padx=15, pady=8, relief="groove", bd=2)
        b_tc.pack(side="left", expand=True, fill="both", padx=4)
        tk.Label(b_tc, text="TIMECODE", font=("Sans", 8), bg="#181818", fg="#aaaaaa").pack()
        self.lbl_timecode = tk.Label(b_tc, text="00:00:00.00", font=("Monospace", 18, "bold"), bg="#181818", fg="#00ff66")
        self.lbl_timecode.pack()

        b_dt = tk.Frame(metrics_frame, bg="#181818", padx=15, pady=8, relief="groove", bd=2)
        b_dt.pack(side="left", expand=True, fill="both", padx=4)
        tk.Label(b_dt, text="FECHA DETECTADA EN CINTA", font=("Sans", 8), bg="#181818", fg="#aaaaaa").pack()
        self.lbl_rec_date = tk.Label(b_dt, text="----.--.-- --:--:--", font=("Monospace", 15, "bold"), bg="#181818", fg="#38bdf8")
        self.lbl_rec_date.pack(pady=2)

        b_sz = tk.Frame(metrics_frame, bg="#181818", padx=15, pady=8, relief="groove", bd=2)
        b_sz.pack(side="left", expand=True, fill="both", padx=4)
        tk.Label(b_sz, text="DATOS CAPTURADOS", font=("Sans", 8), bg="#181818", fg="#aaaaaa").pack()
        self.lbl_data_stats = tk.Label(b_sz, text="0 MiB (0 frames)", font=("Monospace", 14, "bold"), bg="#181818", fg="#fbbf24")
        self.lbl_data_stats.pack(pady=4)

        self.prog_bar = ttk.Progressbar(dash_frame, mode="indeterminate")
        self.prog_bar.pack(fill="x", padx=5, pady=(4, 2))

        # 4. Botonera
        btn_action_frame = ttk.Frame(self.tab_capture, padding=6)
        btn_action_frame.pack(fill="x", padx=12)

        self.btn_start = ttk.Button(btn_action_frame, text="▶ INICIAR CAPTURA", command=self.start_capture)
        self.btn_start.pack(side="left", padx=5, ipady=4)

        self.btn_stop = ttk.Button(btn_action_frame, text="⏹ DETENER CAPTURA", command=self.stop_capture, state="disabled")
        self.btn_stop.pack(side="left", padx=5, ipady=4)

        ttk.Button(btn_action_frame, text="🔍 Probar Enlace FireWire", command=self.check_firewire).pack(side="right", padx=5)

        # 5. Consola detallada de dvgrab y transferencia
        log_frame = ttk.LabelFrame(self.tab_capture, text=" Consola detallada de dvgrab y transferencia ", padding=6)
        log_frame.pack(expand=True, fill="both", padx=12, pady=(4, 10))

        self.txt_capture_log = tk.Text(log_frame, bg="#121212", fg="#00ff00", insertbackground="white", font=("Monospace", 8), height=8)
        self.txt_capture_log.pack(side="left", expand=True, fill="both")
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.txt_capture_log.yview)
        self.txt_capture_log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")

    # ------------------ CONTROL AV/C VCR ------------------
    def send_vcr_command(self, action):
        def run_cmd():
            try:
                cmd = ["dvcont"] + action.split()
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
                self.txt_capture_log.insert("end", f"\n[VCR] Comando '{action}': {res.stdout.strip() or 'OK'}\n")
                self.txt_capture_log.see("end")
            except Exception as e:
                self.txt_capture_log.insert("end", f"\n[VCR] Fallo enviando '{action}': {e}\n")
                self.txt_capture_log.see("end")

        threading.Thread(target=run_cmd, daemon=True).start()

    # ------------------ CATÁLOGO SQLITE ------------------
    def setup_catalog_tab(self):
        form_frame = ttk.LabelFrame(self.tab_catalog, text=" Ficha de Cinta ", padding=10)
        form_frame.pack(side="left", fill="y", padx=10, pady=10)

        ttk.Label(form_frame, text="Código / ID (ej. CINTA01):").pack(anchor="w", pady=(0, 2))
        self.entry_code = ttk.Entry(form_frame, width=28)
        self.entry_code.pack(fill="x", pady=(0, 8))

        ttk.Label(form_frame, text="Título descriptivo:").pack(anchor="w", pady=(0, 2))
        self.entry_title = ttk.Entry(form_frame, width=28)
        self.entry_title.pack(fill="x", pady=(0, 8))

        ttk.Label(form_frame, text="Fecha grabada (Año o Rango):").pack(anchor="w", pady=(0, 2))
        self.entry_date = ttk.Entry(form_frame, width=28)
        self.entry_date.pack(fill="x", pady=(0, 8))

        ttk.Label(form_frame, text="Etiquetas / Personas:").pack(anchor="w", pady=(0, 2))
        self.entry_tags = ttk.Entry(form_frame, width=28)
        self.entry_tags.pack(fill="x", pady=(0, 8))

        ttk.Label(form_frame, text="Estado:").pack(anchor="w", pady=(0, 2))
        self.combo_status = ttk.Combobox(
            form_frame,
            values=["Pendiente", "Capturada (RAW)", "Comprimida (MP4)", "En ZFS Proxmox", "A medio grabar / Incompleta", "Descartada / Inútil"],
            state="readonly"
        )
        self.combo_status.current(0)
        self.combo_status.pack(fill="x", pady=(0, 8))

        ttk.Label(form_frame, text="Notas:").pack(anchor="w", pady=(0, 2))
        self.txt_notes = tk.Text(form_frame, height=5, width=28)
        self.txt_notes.pack(fill="x", pady=(0, 10))

        btn_box = ttk.Frame(form_frame)
        btn_box.pack(fill="x", pady=5)
        ttk.Button(btn_box, text="💾 Guardar / Actualizar", command=self.save_tape).pack(fill="x", pady=2)
        ttk.Button(btn_box, text="🧹 Limpiar Formulario", command=self.clear_form).pack(fill="x", pady=2)
        ttk.Button(btn_box, text="🗑 Eliminar Cinta...", command=self.delete_tape).pack(fill="x", pady=(8, 2))

        table_frame = ttk.Frame(self.tab_catalog, padding=10)
        table_frame.pack(side="right", expand=True, fill="both")

        filter_bar = ttk.Frame(table_frame)
        filter_bar.pack(fill="x", pady=(0, 8))
        ttk.Label(filter_bar, text="Filtrar por estado:").pack(side="left", padx=(0, 5))
        self.combo_filter = ttk.Combobox(
            filter_bar,
            values=["(Todos)", "Pendiente", "Capturada (RAW)", "Comprimida (MP4)", "En ZFS Proxmox", "A medio grabar / Incompleta", "Descartada / Inútil"],
            state="readonly",
            width=26
        )
        self.combo_filter.current(0)
        self.combo_filter.pack(side="left", padx=5)
        self.combo_filter.bind("<<ComboboxSelected>>", lambda e: self.load_tapes())
        ttk.Button(filter_bar, text="🔄 Recargar", command=self.load_tapes).pack(side="left", padx=5)

        columns = ("id", "tape_code", "title", "recorded_date", "tags", "status")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("id", text="ID")
        self.tree.heading("tape_code", text="Código")
        self.tree.heading("title", text="Título")
        self.tree.heading("recorded_date", text="Fecha")
        self.tree.heading("tags", text="Etiquetas")
        self.tree.heading("status", text="Estado")

        self.tree.column("id", width=35, anchor="center")
        self.tree.column("tape_code", width=100, anchor="center")
        self.tree.column("title", width=220)
        self.tree.column("recorded_date", width=90, anchor="center")
        self.tree.column("tags", width=140)
        self.tree.column("status", width=140, anchor="center")

        tree_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side="left", expand=True, fill="both")
        tree_scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)

    # ------------------ PESTAÑA CONVERSIÓN FFMPEG ------------------
    def setup_convert_tab(self):
        conv_frame = ttk.LabelFrame(self.tab_convert, text="Compresión FFmpeg H.264 (desentrelazado a doble frecuencia)", padding=15)
        conv_frame.pack(fill="x", padx=15, pady=10)

        in_frame = ttk.Frame(conv_frame)
        in_frame.pack(fill="x", pady=5)
        ttk.Label(in_frame, text="Carpeta con archivos .dv:").pack(side="left")
        self.entry_conv_in = ttk.Entry(in_frame)
        self.entry_conv_in.insert(0, self.settings.get("conversion_source", DEFAULT_ARCHIVE_DIR))
        self.entry_conv_in.pack(side="left", expand=True, fill="x", padx=5)
        ttk.Button(in_frame, text="Examinar...", command=lambda: self.browse_folder(self.entry_conv_in)).pack(side="right")

        out_frame = ttk.Frame(conv_frame)
        out_frame.pack(fill="x", pady=5)
        ttk.Label(out_frame, text="Carpeta de salida MP4:  ").pack(side="left")
        self.entry_conv_out = ttk.Entry(out_frame)
        self.entry_conv_out.insert(0, self.settings.get("conversion_destination", str(Path.home() / "Videos/MiniDV/Convertidos")))
        self.entry_conv_out.pack(side="left", expand=True, fill="x", padx=5)
        ttk.Button(out_frame, text="Examinar...", command=lambda: self.browse_folder(self.entry_conv_out)).pack(side="right")

        crf_frame = ttk.Frame(conv_frame)
        crf_frame.pack(fill="x", pady=5)
        ttk.Label(crf_frame, text="Calidad CRF:").pack(side="left")
        self.spin_crf = ttk.Spinbox(crf_frame, from_=15, to=24, width=5)
        self.spin_crf.set(18)
        self.spin_crf.pack(side="left", padx=5)

        ttk.Button(conv_frame, text="⚡ Iniciar Conversión por Lotes", command=self.start_manual_conversion).pack(anchor="w", pady=10)

        log_f_frame = ttk.LabelFrame(self.tab_convert, text="Progreso de FFmpeg", padding=10)
        log_f_frame.pack(expand=True, fill="both", padx=15, pady=10)

        self.txt_conv_log = tk.Text(log_f_frame, bg="#121212", fg="#ffffff", insertbackground="white", font=("Monospace", 9))
        self.txt_conv_log.pack(side="left", expand=True, fill="both")
        conv_scroll = ttk.Scrollbar(log_f_frame, orient="vertical", command=self.txt_conv_log.yview)
        self.txt_conv_log.configure(yscrollcommand=conv_scroll.set)
        conv_scroll.pack(side="right", fill="y")

    # ------------------ PESTAÑA DESTINO ZFS PROXMOX ------------------
    def setup_zfs_tab(self):
        zfs_frame = ttk.LabelFrame(self.tab_zfs, text="Carpeta de archivo local o de red", padding=15)
        zfs_frame.pack(fill="x", padx=15, pady=10)

        desc = (
            "Elige una carpeta local, disco externo o recurso de red ya montado.\n"
            "Las cintas se organizan en subcarpetas CODIGO_TITULO."
        )
        ttk.Label(zfs_frame, text=desc, justify="left").pack(anchor="w", pady=(0, 10))

        m_frame = ttk.Frame(zfs_frame)
        m_frame.pack(fill="x", pady=5)
        ttk.Label(m_frame, text="Carpeta de archivo:").pack(side="left")
        self.entry_zfs_path = ttk.Entry(m_frame)
        self.entry_zfs_path.insert(0, self.settings.get("archive_dir", DEFAULT_ARCHIVE_DIR))
        self.entry_zfs_path.pack(side="left", expand=True, fill="x", padx=5)
        ttk.Button(m_frame, text="Examinar...", command=lambda: self.browse_folder(self.entry_zfs_path)).pack(side="right")

        ttk.Button(zfs_frame, text="Probar acceso y espacio libre", command=self.test_zfs_target).pack(anchor="w", pady=10)

        database = ttk.Frame(zfs_frame)
        database.pack(fill="x", pady=8)
        ttk.Label(database, text="Catálogo SQLite:").pack(side="left")
        self.database_label = ttk.Label(database, text=self.db_file, wraplength=600)
        self.database_label.pack(side="left", padx=8)
        ttk.Button(database, text="Elegir…", command=self.choose_database).pack(side="right")
        ttk.Button(zfs_frame, text="Guardar rutas", command=self.persist_settings).pack(anchor="w", pady=6)

    def persist_settings(self):
        self.settings.update(
            capture_dir=self.entry_capture_dir.get().strip(),
            archive_dir=self.entry_zfs_path.get().strip(),
            conversion_source=self.entry_conv_in.get().strip(),
            conversion_destination=self.entry_conv_out.get().strip(),
            database=self.db_file, split_mode=self.split_mode.get(), preview=self.var_preview.get(),
        )
        try:
            save_settings(self.settings)
        except OSError as exc:
            messagebox.showerror("Guardar preferencias", str(exc))

    def choose_database(self):
        if self.is_capturing or self.is_transferring:
            messagebox.showinfo("Proceso activo", "Termina la captura o transferencia antes de cambiar de catálogo.")
            return
        path = filedialog.asksaveasfilename(initialdir=str(Path(self.db_file).parent),
                                          initialfile=Path(self.db_file).name, confirmoverwrite=False,
                                          defaultextension=".db", filetypes=[("Catálogo SQLite", "*.db")])
        if not path:
            return
        previous = self.db_file
        self.db_file = path
        try:
            self.init_database()
            self.load_tapes()
        except sqlite3.Error as exc:
            self.db_file = previous
            messagebox.showerror("Abrir catálogo", str(exc))
            return
        self.database_label.config(text=path)
        self.persist_settings()

    # ------------------ CAPTURA EN VIVO CON PREVIEW ------------------
    def toggle_led(self):
        if not self.is_capturing:
            self.lbl_status_led.config(text="○ EN ESPERA", fg="#777777")
            return
        self.led_state = not self.led_state
        if self.led_state:
            self.lbl_status_led.config(text="● GRABANDO EN CURSO", fg="#ff2222")
        else:
            self.lbl_status_led.config(text="○ GRABANDO EN CURSO", fg="#880000")
        self.after(600, self.toggle_led)

    def parse_dvgrab_line(self, line):
        m_file = re.search(r'"([^"]+\.dv)"', line)
        if m_file:
            self.lbl_active_file.config(text=f"Archivo: {m_file.group(1)}")

        m_tc = re.search(r'timecode\s+([0-9:.]+)', line)
        if m_tc:
            self.lbl_timecode.config(text=m_tc.group(1))

        m_dt = re.search(r'date\s+([0-9._\-\s:]+)', line)
        if m_dt:
            self.last_detected_date = m_dt.group(1).strip()
            self.lbl_rec_date.config(text=self.last_detected_date)

        m_stats = re.search(r':\s+([0-9.]+\s+MiB)\s+([0-9]+\s+frames)', line)
        if m_stats:
            self.lbl_data_stats.config(text=f"{m_stats.group(1)} ({m_stats.group(2)})")

    def start_capture(self):
        if self.is_capturing or self.is_transferring or self.is_converting:
            return
        value = self.entry_capture_dir.get().strip()
        if not value:
            messagebox.showerror("Destino", "Elige una carpeta de captura.")
            return
        out_dir = str(Path(value).expanduser().resolve())
        raw_code = sanitize_folder_name(self.entry_prefix.get().strip())
        if not raw_code or raw_code in {".", ".."}:
            messagebox.showerror("Código de cinta", "Escribe un código de cinta válido.")
            return
        if not shutil.which("dvgrab"):
            messagebox.showerror("Falta dvgrab", "Instala dvgrab para capturar por FireWire.")
            return
        preview = self.var_preview.get()
        if preview and not shutil.which("ffplay"):
            messagebox.showerror("Falta ffplay", "Instala ffplay o desactiva la previsualización.")
            return
        command = capture_command(raw_code if raw_code.endswith("_") else raw_code + "_", self.split_mode.get())
        self.persist_settings()
        raw_title = self.entry_quick_title.get().strip()

        if not os.path.exists(out_dir):
            try:
                os.makedirs(out_dir, exist_ok=True)
            except Exception as e:
                messagebox.showerror("Error de Ruta", f"No se pudo crear la carpeta local:\n{e}")
                return

        self.captured_files_before = set(os.listdir(out_dir))
        self.last_detected_date = ""

        self.txt_capture_log.insert("end", f"\n[+] Iniciando ingesta en disco local: {out_dir}\n")
        self.txt_capture_log.see("end")

        self.is_capturing = True
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.prog_bar.start(15)
        self.toggle_led()

        self.send_vcr_command("play")

        def capture_thread():
            try:
                self.capture_process = subprocess.Popen(
                    command, cwd=out_dir, stdout=subprocess.PIPE if preview else subprocess.DEVNULL,
                    stderr=subprocess.PIPE, start_new_session=True,
                )
                self.preview_process = None
                if not self.is_capturing:
                    self.capture_process.send_signal(signal.SIGINT)
                if preview and self.is_capturing:
                    self.preview_process = subprocess.Popen(
                        ["ffplay", "-window_title", f"MiniDV Video Preview ({raw_code})",
                         "-vf", "bwdif=0", "-x", "480", "-y", "360", "-loglevel", "quiet", "-"],
                        stdin=self.capture_process.stdout, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, start_new_session=True,
                    )
                    self.capture_process.stdout.close()
                for raw_line in self.capture_process.stderr:
                    line = raw_line.decode(errors="replace")
                    self.after(0, self.capture_log, line)
                    self.after(0, self.parse_dvgrab_line, line)

                self.capture_process.wait()
            except Exception as e:
                self.after(0, self.capture_log, f"\n[!] Error en captura: {e}\n")
            finally:
                if self.capture_process and self.capture_process.poll() is None:
                    self.capture_process.send_signal(signal.SIGINT)
                    try:
                        self.capture_process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        self.capture_process.kill()
                        self.capture_process.wait()
                preview_process = getattr(self, "preview_process", None)
                if preview_process and preview_process.poll() is None:
                    preview_process.terminate()
                    try:
                        preview_process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        preview_process.kill()
                        preview_process.wait()
                self.is_capturing = False
                self.after(0, self.on_capture_finished, out_dir, raw_code.rstrip("_"), raw_title)

        threading.Thread(target=capture_thread, daemon=True).start()

    def capture_log(self, line):
        self.txt_capture_log.insert("end", line)
        self.txt_capture_log.see("end")

    def stop_capture(self):
        if self.capture_process and self.is_capturing:
            try:
                os.killpg(os.getpgid(self.capture_process.pid), signal.SIGINT)
            except Exception:
                self.capture_process.terminate()
            self.is_capturing = False

        self.send_vcr_command("stop")

    def on_capture_finished(self, out_dir, tape_code, tape_title):
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.prog_bar.stop()
        self.lbl_status_led.config(text="○ DETENIDO", fg="#777777")
        self.txt_capture_log.insert("end", "\n[✔] Captura detenida.\n")
        self.txt_capture_log.see("end")

        files_after = set(os.listdir(out_dir)) if os.path.exists(out_dir) else set()
        new_dv_files = [
            os.path.join(out_dir, f) for f in (files_after - self.captured_files_before)
            if f.lower().endswith(".dv")
        ]

        rec_date = self.last_detected_date[:10] if self.last_detected_date else ""
        quick_title = tape_title if tape_title else f"Grabación {tape_code}"
        notes = f"Tomas capturadas: {len(new_dv_files)} archivo(s) .dv."

        self.auto_register_tape(tape_code, quick_title, rec_date, notes)

        if new_dv_files and not self.closing:
            self.after(400, lambda: self.ask_post_capture_action(new_dv_files, tape_code, quick_title))

    def auto_register_tape(self, tape_code, title, recorded_date, notes):
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO tapes (tape_code, title, recorded_date, tags, notes, status)
                VALUES (?, ?, ?, 'MiniDV', ?, 'Capturada (RAW)')
                ON CONFLICT(tape_code) DO UPDATE SET
                    title=coalesce(nullif(excluded.title, ''), tapes.title),
                    recorded_date=coalesce(nullif(excluded.recorded_date, ''), tapes.recorded_date),
                    notes=excluded.notes,
                    status='Capturada (RAW)'
            """, (tape_code, title, recorded_date, notes))
            conn.commit()
            conn.close()
            self.load_tapes()
            self.txt_capture_log.insert("end", f"[BBDD] ✔ Ficha '{tape_code}' registrada automáticamente en SQLite.\n")
            self.txt_capture_log.see("end")
        except Exception as e:
            self.txt_capture_log.insert("end", f"[BBDD] ✖ Error guardando en base de datos: {e}\n")

    # ------------------ ACCIONES POST-CAPTURA (CON BARRA DE CONSOLA) ------------------
    def ask_post_capture_action(self, new_files, tape_code, tape_title):
        clean_code = sanitize_folder_name(tape_code)
        clean_title = sanitize_folder_name(tape_title)
        subfolder_name = f"{clean_code}_{clean_title}" if clean_title else clean_code

        base_nfs = str(Path(self.entry_zfs_path.get().strip()).expanduser().resolve())
        self.persist_settings()
        final_target_dir = os.path.join(base_nfs, subfolder_name)

        # Confirmación ligera en línea o diálogo de una sola pregunta
        res = messagebox.askyesno(
            f"Transferir al archivo - {tape_code}",
            f"Se han capturado {len(new_files)} toma(s).\n\n"
            f"¿Mover los archivos ahora a la subcarpeta del archivo?\n"
            f"📁 {final_target_dir}\n\n"
            "(El progreso se mostrará en la consola inferior en tiempo real)"
        )

        if res:
            self.transfer_files_in_console(new_files, final_target_dir, move=True)

    def transfer_files_in_console(self, files_list, target_subfolder, move=True):
        """Transfiere los archivos mostrando la barra de progreso byte a byte dentro de la consola."""
        if self.is_transferring or self.is_capturing:
            messagebox.showwarning("Ocupado", "Ya hay una transferencia de archivos en marcha.")
            return

        action_name = "Moviendo" if move else "Copiando"
        self.is_transferring = True
        self.persist_settings()

        def worker():
            self.is_transferring = True
            try:
                os.makedirs(target_subfolder, exist_ok=True)
            except Exception as e:
                self.txt_capture_log.insert("end", f"\n[!] Error creando subcarpeta destino: {e}\n")
                self.is_transferring = False
                return

            self.txt_capture_log.insert("end", f"\n=== {action_name} {len(files_list)} archivo(s) hacia el archivo: {os.path.basename(target_subfolder)}/ ===\n")
            self.txt_capture_log.see("end")

            buffer_size = 4 * 1024 * 1024  # 4MB para exprimir Gigabit
            bar_width = 24

            for idx, src_file in enumerate(files_list, 1):
                fname = os.path.basename(src_file)
                dst_file = os.path.join(target_subfolder, fname)

                file_size = os.path.getsize(src_file)
                size_mb = file_size / (1024 * 1024)

                self.txt_capture_log.insert("end", f"\n[{action_name} {idx}/{len(files_list)}] {fname} -> {os.path.basename(target_subfolder)}/\n")
                self.txt_capture_log.insert("end", "[                        ]   0.0% |   0.0 MB /   0.0 MB\n")
                self.txt_capture_log.see("end")

                copied_bytes = 0
                start_time = time.time()
                last_refresh = 0

                try:
                    if os.path.exists(dst_file):
                        raise FileExistsError("El destino ya existe. Se conserva el original local.")
                    with open(src_file, 'rb') as f_src, open(dst_file, 'xb') as f_dst:
                        while True:
                            chunk = f_src.read(buffer_size)
                            if not chunk:
                                break
                            f_dst.write(chunk)
                            copied_bytes += len(chunk)

                            now = time.time()
                            # Actualizar consola 5 veces por segundo para máxima fluidez
                            if now - last_refresh > 0.2 or copied_bytes >= file_size:
                                last_refresh = now
                                pct = (copied_bytes / file_size) * 100 if file_size > 0 else 100
                                filled = int(bar_width * (pct / 100))
                                bar = "█" * filled + "░" * (bar_width - filled)
                                elapsed = now - start_time
                                speed = (copied_bytes / (1024 * 1024)) / elapsed if elapsed > 0 else 0
                                done_mb = copied_bytes / (1024 * 1024)

                                line_str = f"[{bar}] {pct:5.1f}% | {done_mb:6.1f} MB / {size_mb:6.1f} MB | {speed:5.1f} MB/s"

                                def update_ui(s=line_str):
                                    self.txt_capture_log.delete("end-2l", "end-1l")
                                    self.txt_capture_log.insert("end", f"\r{s}\n")
                                    self.txt_capture_log.see("end")

                                self.after(0, update_ui)

                    shutil.copystat(src_file, dst_file)
                    if move:
                        os.remove(src_file)

                except Exception as e:
                    self.txt_capture_log.insert("end", f"\n[!] Error transfiriendo {fname}: {e}\n")
                    self.txt_capture_log.see("end")

            self.txt_capture_log.insert("end", f"\n[✔] Todos los archivos transferidos y organizados en: {target_subfolder}\n")
            self.txt_capture_log.see("end")
            self.is_transferring = False

        threading.Thread(target=worker, daemon=True).start()

    def start_manual_conversion(self):
        if self.is_converting or self.is_capturing:
            return
        source = self.entry_conv_in.get().strip()
        destination = self.entry_conv_out.get().strip()
        if not source or not destination:
            messagebox.showerror("Rutas", "Elige las carpetas de origen y destino.")
            return
        in_dir = str(Path(source).expanduser().resolve())
        out_dir = str(Path(destination).expanduser().resolve())
        self.persist_settings()
        try:
            crf = int(self.spin_crf.get())
            if not 15 <= crf <= 24:
                raise ValueError
        except ValueError:
            messagebox.showerror("Calidad", "Elige un CRF entero entre 15 y 24.")
            return
        if not os.path.exists(in_dir):
            messagebox.showerror("Error", "La carpeta de origen no existe.")
            return
        files = [os.path.join(in_dir, f) for f in os.listdir(in_dir) if f.lower().endswith(".dv")]
        if not files:
            messagebox.showinfo("Sin archivos", "No hay archivos .dv en la carpeta.")
            return

        self.is_converting = True

        def convert_thread():
            try:
                os.makedirs(out_dir, exist_ok=True)
                for in_path in files:
                    fname = os.path.basename(in_path)
                    out_path = os.path.join(out_dir, f"{os.path.splitext(fname)[0]}.mp4")
                    cmd = [
                        "ffmpeg", "-nostdin", "-n", "-i", in_path,
                        "-vf", "bwdif=mode=send_field:parity=bff:deint=all",
                        "-c:v", "libx264", "-crf", str(crf), "-preset", "slow", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                        out_path
                    ]
                    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                    if result.returncode:
                        self.after(0, lambda path=fname: self.txt_conv_log.insert("end", f"\nNo convertido o ya existente: {path}\n"))
                self.after(0, lambda: self.txt_conv_log.insert("end", "\nConversión finalizada. Revisa los avisos.\n"))
            except OSError as exc:
                self.after(0, lambda text=str(exc): self.txt_conv_log.insert("end", text + "\n"))
            finally:
                self.is_converting = False

        threading.Thread(target=convert_thread, daemon=True).start()

    # ------------------ UTILIDADES Y CATÁLOGO ------------------
    def browse_folder(self, target_entry):
        folder = filedialog.askdirectory(initialdir=target_entry.get())
        if folder:
            target_entry.delete(0, "end")
            target_entry.insert(0, folder)
            self.persist_settings()

    def check_firewire(self):
        try:
            res_pci = subprocess.run(["lspci", "-k"], capture_output=True, text=True)
            res_nodes = subprocess.run(["ls", "-l", "/dev/fw0"], capture_output=True, text=True)
            info = []
            if "firewire" in res_pci.stdout.lower():
                info.append("✔ Controlador IEEE 1394 activo en el kernel.")
            else:
                info.append("✖ No se detecta controladora FireWire.")

            if res_nodes.returncode == 0:
                info.append("✔ Dispositivo /dev/fw0 conectado (Videocámara online).")
            else:
                info.append("⚠ /dev/fw0 no disponible. Revisa el cable o pon la cámara en VCR/Play.")
            messagebox.showinfo("Estado FireWire", "\n".join(info))
        except Exception as e:
            messagebox.showerror("Error", f"Fallo al comprobar FireWire: {e}")

    def test_zfs_target(self):
        target = self.entry_zfs_path.get().strip()
        if os.path.exists(target) and os.path.isdir(target):
            try:
                st = os.statvfs(target)
                free_gb = (st.f_bavail * st.f_frsize) / (1024 ** 3)
                messagebox.showinfo("Almacenamiento", f"✔ Carpeta disponible:\n{target}\n\nEspacio disponible: {free_gb:.1f} GB")
            except Exception:
                messagebox.showinfo("Almacenamiento", f"✔ Conectado a la ruta:\n{target}")
        else:
            messagebox.showwarning("Atención", f"La ruta no existe o no está montada:\n{target}")

    def load_tapes(self):
        for row in self.tree.get_children():
            self.tree.delete(row)

        filter_status = self.combo_filter.get() if hasattr(self, 'combo_filter') else "(Todos)"
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()

        if filter_status and filter_status != "(Todos)":
            cursor.execute("SELECT id, tape_code, title, recorded_date, tags, status FROM tapes WHERE status = ? ORDER BY tape_code ASC", (filter_status,))
        else:
            cursor.execute("SELECT id, tape_code, title, recorded_date, tags, status FROM tapes ORDER BY tape_code ASC")

        for record in cursor.fetchall():
            self.tree.insert("", "end", values=record)
        conn.close()

    def save_tape(self):
        code = self.entry_code.get().strip()
        title = self.entry_title.get().strip()
        rec_date = self.entry_date.get().strip()
        tags = self.entry_tags.get().strip()
        status = self.combo_status.get()
        notes = self.txt_notes.get("1.0", "end-1c").strip()

        if not code or not title:
            messagebox.showwarning("Atención", "El código y el título son obligatorios.")
            return

        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO tapes (tape_code, title, recorded_date, tags, notes, status)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(tape_code) DO UPDATE SET
                title=excluded.title,
                recorded_date=excluded.recorded_date,
                tags=excluded.tags,
                notes=excluded.notes,
                status=excluded.status
        """, (code, title, rec_date, tags, notes, status))
        conn.commit()
        conn.close()
        self.load_tapes()
        messagebox.showinfo("Éxito", f"Cinta {code} guardada en el catálogo.")

    def delete_tape(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("Atención", "Selecciona una cinta del catálogo.")
            return
        tape_id = self.tree.item(selected[0])["values"][0]
        tape_code = self.tree.item(selected[0])["values"][1]

        res = messagebox.askyesnocancel(
            "Eliminar Cinta",
            f"¿Deseas eliminar la ficha de la cinta '{tape_code}' del catálogo?\n\n"
            "• SÍ: Elimina la ficha Y borra los archivos .dv asociados en disco/NFS.\n"
            "• NO: Elimina solo la ficha de la BBDD (conserva los archivos).\n"
            "• CANCELAR: No hacer nada."
        )

        if res is None:
            return

        delete_files_too = res
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM tapes WHERE id = ?", (tape_id,))
        conn.commit()
        conn.close()

        deleted_files_count = 0
        if delete_files_too:
            search_paths = list(dict.fromkeys([self.entry_capture_dir.get().strip(), self.entry_zfs_path.get().strip()]))
            for p in search_paths:
                if os.path.exists(p):
                    pattern_files = os.path.join(p, f"{tape_code}*.dv")
                    for f in glob.glob(pattern_files):
                        try:
                            os.remove(f)
                            deleted_files_count += 1
                        except Exception:
                            pass
                    pattern_dirs = os.path.join(p, f"{tape_code}_*")
                    for d in glob.glob(pattern_dirs):
                        if os.path.isdir(d):
                            try:
                                count = len([f for f in os.listdir(d) if f.endswith('.dv')])
                                shutil.rmtree(d)
                                deleted_files_count += count
                            except Exception:
                                pass

        self.clear_form()
        self.load_tapes()
        messagebox.showinfo("Completado", f"Ficha {tape_code} eliminada (Archivos borrados: {deleted_files_count}).")

    def clear_form(self):
        self.entry_code.delete(0, "end")
        self.entry_title.delete(0, "end")
        self.entry_date.delete(0, "end")
        self.entry_tags.delete(0, "end")
        self.combo_status.current(0)
        self.txt_notes.delete("1.0", "end")

    def on_tree_select(self, event):
        selected = self.tree.selection()
        if not selected:
            return
        tape_id = self.tree.item(selected[0])["values"][0]
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        cursor.execute("SELECT tape_code, title, recorded_date, tags, notes, status FROM tapes WHERE id = ?", (tape_id,))
        row = cursor.fetchone()
        conn.close()
        if row:
            self.clear_form()
            self.entry_code.insert(0, row[0] or "")
            self.entry_title.insert(0, row[1] or "")
            self.entry_date.insert(0, row[2] or "")
            self.entry_tags.insert(0, row[3] or "")
            self.txt_notes.insert("1.0", row[4] or "")
            if row[5] in self.combo_status["values"]:
                self.combo_status.set(row[5])

    def on_closing(self):
        self.persist_settings()
        if self.is_transferring or self.is_converting:
            messagebox.showinfo("Proceso activo", "Espera a que termine la transferencia o conversión antes de cerrar.")
            return
        if self.is_capturing:
            if messagebox.askyesno("Proceso activo", "¿Detener la captura y cerrar?"):
                self.closing = True
                self.stop_capture()
                self.after(100, self.finish_closing)
            return
        self.destroy()

    def finish_closing(self):
        if self.capture_process and self.capture_process.poll() is None:
            self.after(100, self.finish_closing)
        else:
            self.destroy()

if __name__ == "__main__":
    app = MiniDVApp()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()
