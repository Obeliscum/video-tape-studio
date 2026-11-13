# Copyright (c) 2026 Obeliscum. Licensed under the MIT License.
"""Montaje local de recuerdos a partir de carpetas de vídeo."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
import json
import math
from pathlib import Path
from queue import Empty, Queue
import re
import shutil
import stat
import subprocess
import tempfile
import textwrap
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from preferences import load_settings, save_settings, output_path

FPS = 25
MIN_CLIP_SECONDS = 3
TRANSITIONS = {
    "Fundidos variados": ("black", "white"),
    "Fundido a negro": ("black",),
    "Fundido a blanco": ("white",),
    "Cortes directos": (None,),
}
TITLE_STYLES = {
    "Títulos variados": ("center", "lower_left", "lower_center"),
    "Centrado clásico": ("center",),
    "Rótulo a la izquierda": ("lower_left",),
    "Banda inferior centrada": ("lower_center",),
}
TITLE_LAYOUTS = {
    "center": ("(w-tw)/2", "(h-th)/2", "Serif", 40, "white", "0x10243b@0.78"),
    "lower_left": ("46", "h-th-52", "Sans", 34, "0xffe1a6", "0x10243b@0.82"),
    "lower_center": ("(w-tw)/2", "h-th-46", "Sans", 34, "white", "black@0.68"),
}
VIDEO_TYPES = {".dv", ".mp4", ".mov", ".m4v", ".mkv", ".avi", ".mts", ".m2ts", ".webm", ".mpg", ".mpeg", ".3gp"}
AUDIO_TYPES = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".aiff", ".m4b"}
BASE = ["ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "warning",
        "-progress", "pipe:1", "-y"]
WIDTH, HEIGHT = 1024, 576
AUDIO_RATE = 48000
DATE_PATTERN = re.compile(r"(?<!\d)(\d{4})[-_:.]?(\d{2})[-_:.]?(\d{2})(?!\d)")


@dataclass(frozen=True)
class Clip:
    path: Path
    frames: int
    audio: bool
    interlace: str


@dataclass
class Chapter:
    title: str
    clips: list[Clip]

    @property
    def frames(self):
        return sum(clip.frames for clip in self.clips)


def sort_key(path):
    return [int(part) if part.isdigit() else part.casefold()
            for part in re.split(r"(\d+)", str(path))]


def date_in(value):
    for match in DATE_PATTERN.finditer(value):
        try:
            candidate = date(*map(int, match.groups()))
            if 1900 <= candidate.year and candidate <= date.today():
                return candidate
        except ValueError:
            continue
    return None


def folder_label(folder, root):
    parts = (root.name, *folder.relative_to(root).parts)
    result = []
    for part in parts:
        label = DATE_PATTERN.sub("", part).replace("_", " ").strip(" -.")
        if label and label not in result:
            result.append(label)
    return " / ".join(result)


def original_dv(path):
    if path.suffix.lower() != ".mp4":
        return None
    folder = path.parent.parent if path.parent.name.casefold() == "codificados" else None
    settings = load_settings()
    destination = settings.get("encoded_dir")
    source = settings.get("source_dir")
    if folder is None and destination and source:
        try:
            relative = path.parent.relative_to(Path(destination))
            folder = Path(source) / relative
        except ValueError:
            pass
    if folder is None:
        return None
    matches = [folder / (path.stem + suffix) for suffix in (".dv", ".DV")
               if (folder / (path.stem + suffix)).is_file()]
    return matches[0] if len(matches) == 1 else None


def file_identity(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.resolve() != path:
        raise ValueError("La ruta ya no apunta a un archivo regular sin enlaces.")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@dataclass(frozen=True)
class RejectedFile:
    path: Path
    reason: str
    identity: tuple[int, ...]


def rejected_versions(path, reason):
    if path.suffix.lower() == ".dv":
        candidates = [path.parent / "codificados" / (path.stem + ".mp4")]
        settings = load_settings()
        if settings.get("source_dir") and settings.get("encoded_dir"):
            try:
                candidates.append(output_path(path, Path(settings["source_dir"]), Path(settings["encoded_dir"])))
            except ValueError:
                pass
        for candidate in candidates:
            if original_dv(candidate) == path:
                try:
                    yield RejectedFile(candidate, reason, file_identity(candidate))
                except (OSError, ValueError):
                    pass
        pair = None
    else:
        pair = original_dv(path)
    for candidate in (path, pair):
        if candidate is not None:
            try:
                yield RejectedFile(candidate, reason, file_identity(candidate))
            except (OSError, ValueError):
                continue


def inputs(roots, check_cancel):
    seen = set()
    for root in roots:
        files = []
        for path in root.rglob("*"):
            check_cancel()
            if path.suffix.lower() in VIDEO_TYPES and path.is_file():
                files.append(path)
        for path in sorted(files, key=sort_key):
            original = original_dv(path)
            if original and any(original.is_relative_to(selected) for selected in roots):
                continue
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                yield root, path


def recording_metadata(paths, check_cancel):
    if not shutil.which("exiftool"):
        return {}
    metadata = {}
    for offset in range(0, len(paths), 64):
        check_cancel()
        try:
            result = subprocess.run(
                ["exiftool", "-j", "-DateTimeOriginal", "-LocationName", "-City",
                 *paths[offset:offset + 64]], capture_output=True, text=True,
                errors="replace", timeout=45,
            )
            if result.stdout.strip():
                for entry in json.loads(result.stdout):
                    metadata[Path(entry["SourceFile"])] = entry
        except (KeyError, ValueError, OSError, subprocess.TimeoutExpired):
            continue
    return metadata


def probe(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:format_tags=creation_time,date:"
         "stream=codec_type,codec_name,field_order,duration:stream_disposition=attached_pic",
         "-of", "json", path],
        capture_output=True, text=True, errors="replace", timeout=25, check=True,
    )
    return json.loads(result.stdout)


def has_visible_image(path, duration, check_cancel):
    """Muestrea fotogramas repartidos y conserva el clip si alguno tiene detalle visible."""
    width, height = 64, 36
    sample_size = width * height
    positions = (0.5, 0.1, 0.9) if duration < 8 else (0.5, 0.1, 0.9, 0.3, 0.7)
    for position in positions:
        check_cancel()
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-nostdin", "-threads", "1",
             "-ss", f"{duration * position:.3f}", "-i", str(path), "-map", "0:V:0",
             "-filter_threads", "1", "-vf", f"scale={width}:{height}:flags=area,format=rgb24",
             "-frames:v", "1", "-threads:v", "1", "-f", "rawvideo", "pipe:1"],
            capture_output=True, timeout=15, check=True,
        )
        if len(result.stdout) == sample_size * 3:
            rgb = result.stdout
            visible_pixels = sum(
                max(red, green, blue) >= 36 for red, green, blue in zip(rgb[::3], rgb[1::3], rgb[2::3])
            )
            if visible_pixels * 100 >= 3 * sample_size:
                return True
    return False


def analyze(roots, check_cancel, report):
    files = list(inputs(roots, check_cancel))
    report("Leyendo fechas y lugares guardados en los archivos…", 0)
    originals = {path: original_dv(path) for _, path in files}
    metadata = recording_metadata(
        list(dict.fromkeys(p for path, original in originals.items() for p in (path, original) if p)),
        check_cancel,
    )
    groups = defaultdict(list)
    skipped = 0
    too_short = 0
    too_dark = 0
    rejected = {}
    for index, (root, path) in enumerate(files, 1):
        check_cancel()
        report(f"Analizando {index}/{len(files)} · {path.name}", 100 * index / len(files))
        try:
            info = probe(path)
            streams = info.get("streams", [])
            video = next(s for s in streams if s["codec_type"] == "video"
                         and not s.get("disposition", {}).get("attached_pic"))
            container = info.get("format", {})
            video_duration = video.get("duration")
            duration = float(video_duration if video_duration not in {None, "N/A"} else container["duration"])
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError
            if duration < MIN_CLIP_SECONDS:
                too_short += 1
                for version in rejected_versions(path, f"Menos de 3 s ({duration:.2f} s)"):
                    rejected[version.path] = version
                continue
            if not has_visible_image(path, duration, check_cancel):
                too_dark += 1
                for version in rejected_versions(path, "Sin imagen visible"):
                    rejected[version.path] = version
                continue
            frames = math.floor(duration * FPS + 1e-6)
        except (StopIteration, KeyError, ValueError, OverflowError, OSError, subprocess.SubprocessError):
            skipped += 1
            continue
        tags = container.get("tags", {})
        source_folder = path.parent
        if source_folder.name.casefold() == "codificados":
            source_folder = source_folder.parent
        display_root = root.parent if root.name.casefold() == "codificados" else root
        item_meta = metadata.get(path, {})
        dv_meta = metadata.get(originals[path], {})
        # La fecha del nombre tiene preferencia sobre la fecha de creación del MP4.
        recorded = (date_in(str(dv_meta.get("DateTimeOriginal", "")))
                    or date_in(str(item_meta.get("DateTimeOriginal", ""))) or date_in(path.stem)
                    or date_in(str(source_folder.relative_to(display_root))) or date_in(display_root.name)
                    or date_in(tags.get("date", "")) or date_in(tags.get("creation_time", "")))
        location = (item_meta.get("LocationName") or item_meta.get("City")
                    or folder_label(source_folder, display_root))
        field = video.get("field_order", "")
        interlace = "bff" if video["codec_name"] == "dvvideo" else "auto" if field in {
            "tt", "bb", "tb", "bt"
        } else ""
        groups[root, source_folder, recorded, str(location).strip()].append(Clip(
            path, frames, any(s["codec_type"] == "audio" for s in streams), interlace))
    chapters = []
    for (_, _, recorded, location), clips in sorted(
        groups.items(), key=lambda entry: (roots.index(entry[0][0]),
                                           entry[0][2] or date.max, sort_key(entry[0][1]))
    ):
        formatted = recorded.strftime("%d/%m/%Y") if recorded else ""
        title = " · ".join(value for value in (location, formatted) if value)
        chapters.append(Chapter(title or f"Capítulo {len(chapters) + 1}", clips))
    return chapters, skipped, too_short, too_dark, rejected


def montage_plan(chapters, limit):
    # Reparte el presupuesto en ventanas a lo largo de cada capítulo, en orden.
    total = sum(c.frames for c in chapters)
    if len(chapters) > limit:
        raise ValueError("Hay demasiados capítulos para la duración elegida.")
    budgets = [1] * len(chapters)
    remaining = min(total, limit) - len(chapters)
    capacity = total - len(chapters)
    if capacity:
        cumulative = 0
        for index, chapter in enumerate(chapters):
            previous = cumulative
            cumulative += chapter.frames - 1
            budgets[index] += cumulative * remaining // capacity - previous * remaining // capacity
    for chapter, budget in zip(chapters, budgets):
        available = chapter.frames
        windows = math.ceil(budget / (12 * FPS)) if budget < available else 1
        offsets = []
        for index in range(windows):
            left = available * index // windows
            right = available * (index + 1) // windows
            length = budget * (index + 1) // windows - budget * index // windows
            start = left + (right - left - length) // 2
            offsets.append((start, start + length))
        segments = []
        position = 0
        window_index = 0
        for clip in chapter.clips:
            end = position + clip.frames
            while window_index < len(offsets):
                start, stop = offsets[window_index]
                if start >= end:
                    break
                cut_start, cut_end = max(start, position), min(stop, end)
                if cut_end > cut_start:
                    segments.append((clip, cut_start - position, cut_end - cut_start))
                if stop > end:
                    break
                window_index += 1
            position = end
        yield chapter.title, segments


def video_segment(clip, start, frames, target, title_path, title_seconds, title_style,
                  fade_in, fade_out):
    duration = frames / FPS
    cmd = BASE.copy()
    if clip:
        cmd += ["-ss", f"{start / FPS:.3f}", "-i", str(clip.path)]
    else:
        cmd += ["-f", "lavfi", "-i", f"color=c=0x10243b:s={WIDTH}x{HEIGHT}:r={FPS}"]
    if not clip or not clip.audio:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    filters = []
    if clip and clip.interlace:
        filters.append(f"bwdif=mode=send_frame:parity={clip.interlace}:deint=all")
    filters.extend([
        f"fps={FPS}",
        f"scale=w='max(2,trunc(min({WIDTH},{HEIGHT}*dar)/2)*2)':"
        f"h='max(2,trunc(min({HEIGHT},{WIDTH}/dar)/2)*2)'",
        "setsar=1", f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2", "format=yuv420p",
        f"tpad=stop_mode=clone:stop_duration={duration:.3f}",
    ])
    if title_path:
        x, y, font, size, foreground, background = TITLE_LAYOUTS[title_style]
        filters.append(
            f"drawtext=font={font}:textfile={title_path}:expansion=none:fontsize={size}:"
            f"fontcolor={foreground}:line_spacing=8:box=1:boxcolor={background}:boxborderw=14:"
            f"x={x}:y={y}:enable='lt(t,{title_seconds:.3f})'"
        )
    audio = ["aresample=48000", "aformat=channel_layouts=stereo", "apad"]
    transition = min(0.55, duration / 3)
    if fade_in:
        filters.append(f"fade=t=in:st=0:d={transition:.3f}:color={fade_in}")
        audio.append(f"afade=t=in:st=0:d={transition:.3f}")
    if fade_out:
        # El último fotograma alcanza el mismo color con el que empieza el capítulo siguiente.
        at = max(0, duration - transition - 1 / FPS)
        filters.append(f"fade=t=out:st={at:.3f}:d={transition:.3f}:color={fade_out}")
        audio.append(f"afade=t=out:st={at:.3f}:d={transition:.3f}")
    return cmd + [
        "-filter_threads", "2", "-map", "0:V:0", "-map", "0:a:0" if clip and clip.audio else "1:a:0",
        "-vf", ",".join(filters), "-af", ",".join(audio), "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-threads:v", "4", "-preset", "veryfast", "-crf", "19", "-bf", "0",
        "-c:a", "pcm_s16le", str(target),
    ]


def concat_list(path, files):
    path.write_text("".join(f"file '{file.name}'\n" for file in files), encoding="utf-8")
    return str(path)


def write_title(path, value):
    lines = [line for paragraph in value.splitlines() for line in textwrap.wrap(paragraph, width=34)]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def render_reel(chapters, title, minutes, transitions, title_styles, music_dir,
                output, run, check_cancel, report):
    limit = minutes * 60 * FPS
    intro = min(3 * FPS, limit // 10)
    plan = list(montage_plan(chapters, limit - intro))
    boundaries = [transitions[i % len(transitions)] for i in range(len(plan))]
    total_frames = intro + sum(frames for _, segments in plan for _, _, frames in segments)
    total_seconds = total_frames / FPS
    # Los vídeos temporales pueden ocupar varios GB. Solo el texto va a /tmp,
    # con una ruta sencilla para el filtro drawtext.
    with (tempfile.TemporaryDirectory(prefix=".dvrecuerdo-", dir=output.parent) as scratch,
          tempfile.TemporaryDirectory(prefix="dvrecuerdo-titulos-", dir="/tmp") as captions):
        scratch = Path(scratch)
        captions = Path(captions)
        clips = []
        completed = 0

        def encode(clip, start, frames, caption, show, style, fade_in, fade_out):
            nonlocal completed
            check_cancel()
            segment = scratch / f"v{len(clips):05}.mkv"

            def progress(values):
                try:
                    seconds = int(values.get("out_time_us", "0")) / 1_000_000
                except ValueError:
                    return
                frames_done = completed + max(0, min(frames, seconds * FPS))
                report(f"Montando fragmento {len(clips) + 1}", 85 * frames_done / total_frames)

            run(video_segment(clip, start, frames, segment, caption, show,
                              style, fade_in, fade_out), progress)
            clips.append(segment)
            completed += frames

        opening = write_title(captions / "opening.txt", title)
        encode(None, 0, intro, opening, intro / FPS, title_styles[0], boundaries[0], boundaries[0])
        for index, (chapter, segments) in enumerate(plan, 1):
            caption = write_title(captions / f"chapter{index}.txt", chapter) if chapter else None
            remaining_caption = 4 * FPS
            style = title_styles[index % len(title_styles)]
            enter = boundaries[index - 1]
            leave = boundaries[index] if index < len(plan) else enter
            for position, (clip, start, frames) in enumerate(segments):
                encode(clip, start, frames, caption if remaining_caption > 0 else None,
                       min(frames, remaining_caption) / FPS, style,
                       enter if position == 0 else None,
                       leave if position == len(segments) - 1 else None)
                remaining_caption = max(0, remaining_caption - frames)

        cmd = BASE + ["-f", "concat", "-safe", "1", "-i",
                      concat_list(scratch / "video.txt", clips)]
        if music_dir:
            report("Preparando la música…", 86)
            tracks = []
            remaining = total_seconds
            for path in sorted(music_dir.rglob("*"), key=sort_key):
                if path.suffix.lower() not in AUDIO_TYPES or not path.is_file():
                    continue
                check_cancel()
                track = scratch / f"a{len(tracks):05}.wav"
                try:
                    run(BASE + ["-i", str(path), "-vn", "-map", "0:a:0",
                                "-t", f"{remaining:.3f}", "-ac", "2", "-ar", str(AUDIO_RATE),
                                "-c:a", "pcm_s16le", str(track)], lambda _: None)
                    length = float(probe(track)["format"]["duration"])
                except (RuntimeError, KeyError, ValueError, subprocess.SubprocessError):
                    report(f"No se pudo leer la pista {path.name}", 86)
                    continue
                if length > 0:
                    tracks.append(track)
                    remaining -= length
                if remaining <= 0:
                    break
            if not tracks:
                raise ValueError("La carpeta de música no contiene pistas de audio que se puedan leer.")
            # Un WAV permite a FFmpeg volver al inicio de toda la selección de música.
            playlist = tracks[0]
            if len(tracks) > 1:
                playlist = scratch / "playlist.wav"
                check_cancel()
                run(BASE + ["-f", "concat", "-safe", "1", "-i",
                            concat_list(scratch / "music.txt", tracks),
                            "-c:a", "copy", str(playlist)], lambda _: None)
            music_samples = total_frames * AUDIO_RATE // FPS
            cmd += ["-stream_loop", "-1", "-i", str(playlist), "-filter_complex",
                    "[0:a]asplit=2[original][trigger];"
                    "[trigger]highpass=f=160,lowpass=f=3800[voiceband];"
                    f"[1:a]atrim=end_sample={music_samples},asetpts=N/SR/TB,"
                    "volume=0.20,afade=t=in:d=0.7,"
                    f"afade=t=out:st={max(0, total_seconds - 1.5):.3f}:d=1.5[bed];"
                    "[bed][voiceband]sidechaincompress=threshold=0.025:ratio=8:attack=20:release=500[duck];"
                    "[original][duck]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95:level=0[mix]",
                    "-map", "0:v:0", "-map", "[mix]"]
        else:
            cmd += ["-map", "0:v:0", "-map", "0:a:0"]
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".recuerdo-",
                                         suffix=".mp4", delete=False) as handle:
            temporary = Path(handle.name)
        try:
            check_cancel()
            report("Exportando el MP4…", 94)
            run(cmd + ["-t", f"{total_seconds:.3f}", "-c:v", "copy", "-c:a", "aac",
                       "-b:a", "192k", "-movflags", "+faststart", str(temporary)], lambda _: None)
            check_cancel()
            info = probe(temporary)
            if abs(float(info["format"]["duration"]) - total_seconds) > 0.5:
                raise ValueError("La duración del MP4 exportado no coincide con el montaje.")
            check_cancel()
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)


class RecuerdoWindow(tk.Toplevel):
    def __init__(self, parent, inhibit, encode, cancelled):
        super().__init__(parent)
        self.title("Crear recuerdo · DV Encoder Studio")
        self.geometry("800x675")
        self.minsize(660, 560)
        self.transient(parent)
        self.grab_set()
        self.inhibit = inhibit
        self.encode = encode
        self.cancelled = cancelled
        self.stop = threading.Event()
        self.events = Queue()
        self.chapters = []
        self.rejected = {}
        self.busy = False
        self.close_after = False
        self.saved = load_settings()
        self.initial_folder = self.saved.get("recuerdo_last_source", parent.entry_root.get().strip())
        self.music = tk.StringVar(value=self.saved.get("music_dir", ""))
        self.output_dir = tk.StringVar(value=self.saved.get("recuerdo_output_dir", ""))
        self.film_title = tk.StringVar()
        self.minutes = tk.StringVar(value="12")
        self.transition = tk.StringVar(value=next(iter(TRANSITIONS)))
        self.title_style = tk.StringVar(value=next(iter(TITLE_STYLES)))
        self.status = tk.StringVar(value="Añade las carpetas del viaje y pulsa Analizar carpetas.")
        self.controls = []
        self.create_controls()
        for folder in self.saved.get("recuerdo_sources", []):
            if isinstance(folder, str) and Path(folder).is_dir():
                self.folders.insert("end", folder)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(100, self.poll)

    def button(self, parent, label, command):
        widget = ttk.Button(parent, text=label, command=command)
        self.controls.append(widget)
        return widget

    def create_controls(self):
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)

        footer = ttk.Frame(body)
        footer.pack(side="bottom", fill="x", pady=(8, 0))
        actions = ttk.Frame(footer)
        actions.pack(fill="x")
        self.button(actions, "Generar vídeo…", self.start_render).pack(side="right")
        self.rejected_button = ttk.Button(actions, text="Revisar descartados…",
                                          command=self.review_rejected, state="disabled")
        self.rejected_button.pack(side="left")
        self.cancel_button = ttk.Button(actions, text="Cancelar tarea", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="right", padx=6)
        ttk.Label(footer, textvariable=self.status, wraplength=640).pack(fill="x", pady=(8, 2))
        self.progress = ttk.Progressbar(footer, maximum=100)
        self.progress.pack(fill="x")

        source = ttk.LabelFrame(body, text="1. Carpetas de vídeo", padding=8)
        source.pack(fill="x")
        self.folders = tk.Listbox(source, height=4, selectmode="extended", exportselection=False)
        self.folders.pack(side="left", fill="both", expand=True)
        actions = ttk.Frame(source, padding=(8, 0))
        actions.pack(side="right", fill="y")
        self.button(actions, "Añadir carpeta…", self.add_folder).pack(fill="x")
        self.button(actions, "Quitar selección", self.remove_folder).pack(fill="x", pady=4)
        self.button(actions, "Analizar carpetas", self.start_analysis).pack(fill="x")

        options = ttk.LabelFrame(body, text="2. Título, estilo y música", padding=8)
        options.pack(fill="x", pady=8)
        options.columnconfigure(1, weight=1)
        ttk.Label(options, text="Título del vídeo").grid(row=0, column=0, sticky="w")
        ttk.Entry(options, textvariable=self.film_title).grid(row=0, column=1, columnspan=2,
                                                              sticky="ew", padx=(8, 0))
        ttk.Label(options, text="Duración máxima (min)").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Spinbox(options, from_=1, to=60, textvariable=self.minutes, width=5).grid(
            row=1, column=1, sticky="w", padx=(8, 0))
        for row, label, variable, choices in (
            (2, "Transición entre capítulos", self.transition, TRANSITIONS),
            (3, "Estilo de títulos", self.title_style, TITLE_STYLES),
        ):
            ttk.Label(options, text=label).grid(row=row, column=0, sticky="w", pady=3)
            combo = ttk.Combobox(options, textvariable=variable, state="readonly",
                                 width=26, values=tuple(choices))
            combo.grid(row=row, column=1, sticky="w", padx=(8, 0))
            self.controls.append(combo)
        ttk.Label(options, text="Carpeta de música").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Entry(options, textvariable=self.music).grid(row=4, column=1, sticky="ew", padx=8)
        self.button(options, "Elegir…", self.choose_music).grid(row=4, column=2)

        ttk.Label(options, text="Guardar recuerdos en").grid(row=5, column=0, sticky="w", pady=4)
        ttk.Entry(options, textvariable=self.output_dir).grid(row=5, column=1, sticky="ew", padx=8)
        self.button(options, "Elegir…", self.choose_output).grid(row=5, column=2)

        chapter_box = ttk.LabelFrame(body, text="3. Capítulos propuestos", padding=8)
        chapter_box.pack(fill="both", expand=True)
        ttk.Label(chapter_box, text="Haz doble clic en un título para editarlo.").pack(anchor="w", pady=(0, 5))
        self.button(chapter_box, "Editar título…", self.edit_title).pack(
            side="bottom", anchor="w", pady=(5, 0))
        table = ttk.Frame(chapter_box)
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, show="headings", selectmode="browse",
                                 columns=("title", "clips"))
        self.tree.heading("title", text="Título del capítulo")
        self.tree.heading("clips", text="Clips")
        self.tree.column("title", width=450)
        self.tree.column("clips", width=75, anchor="center", stretch=False)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Double-1>", self.edit_clicked)

    def add_folder(self):
        folder = filedialog.askdirectory(parent=self, initialdir=self.initial_folder, mustexist=True)
        if folder and folder not in self.folders.get(0, "end"):
            self.folders.insert("end", folder)
            self.initial_folder = folder
            if not self.film_title.get().strip():
                title_root = Path(folder)
                if title_root.name.casefold() == "codificados":
                    title_root = title_root.parent
                self.film_title.set(folder_label(title_root, title_root)[:140] or "Recuerdos del viaje")
            self.invalidate_analysis()

    def remove_folder(self):
        selection = self.folders.curselection()
        for index in reversed(selection):
            self.folders.delete(index)
        if selection:
            self.invalidate_analysis()

    def invalidate_analysis(self):
        self.chapters = []
        self.rejected = {}
        self.tree.delete(*self.tree.get_children())
        self.update_rejected_button()
        self.status.set("Pulsa Analizar carpetas para preparar los títulos.")
        self.progress.config(value=0)

    def choose_music(self):
        folder = filedialog.askdirectory(parent=self, initialdir=self.music.get() or self.initial_folder,
                                        mustexist=True)
        if folder:
            self.music.set(folder)

    def choose_output(self):
        folder = filedialog.askdirectory(parent=self, initialdir=self.output_dir.get() or self.initial_folder,
                                        mustexist=True)
        if folder:
            self.output_dir.set(folder)

    def persist_paths(self):
        try:
            save_settings(recuerdo_sources=list(self.folders.get(0, "end")),
                          recuerdo_last_source=self.initial_folder,
                          music_dir=self.music.get().strip(), recuerdo_output_dir=self.output_dir.get().strip())
        except OSError as exc:
            self.status.set(f"No se pudieron recordar las rutas: {exc}")

    def edit_clicked(self, event):
        if row := self.tree.identify_row(event.y):
            self.tree.selection_set(row)
            self.edit_title()

    def edit_title(self):
        selection = self.tree.selection()
        if self.busy or not selection:
            return
        chapter = self.chapters[int(selection[0])]
        title = simpledialog.askstring("Título de capítulo", "Lugar, fecha o título del capítulo:",
                                       initialvalue=chapter.title, parent=self)
        if title is not None:
            if len(title) > 140:
                messagebox.showerror("Título demasiado largo", "Usa hasta 140 caracteres.", parent=self)
                return
            chapter.title = title.strip()
            self.tree.item(selection[0], values=(chapter.title, len(chapter.clips)))

    def check_cancel(self):
        if self.stop.is_set():
            raise self.cancelled

    def report(self, message, percent):
        self.events.put(("progress", (message, percent)))

    def start_task(self, target):
        self.stop.clear()
        self.busy = True
        for control in self.controls:
            control.state(["disabled"])
        self.cancel_button.state(["!disabled"])
        self.rejected_button.state(["disabled"])
        self.progress.config(value=0)

        def work():
            try:
                result = target()
            except self.cancelled:
                self.events.put(("cancelled", None))
            except Exception as exc:
                self.events.put(("error", getattr(exc, "stderr", None) or str(exc)))
            else:
                self.events.put(("done", result))

        threading.Thread(target=work).start()

    def start_analysis(self):
        roots = [Path(folder).resolve() for folder in self.folders.get(0, "end")]
        roots = list(dict.fromkeys(roots))
        if not roots:
            messagebox.showinfo("Carpetas de vídeo", "Añade al menos una carpeta.", parent=self)
            return
        for tool in ("ffprobe", "ffmpeg"):
            if not shutil.which(tool):
                messagebox.showerror("Falta un programa", f"Se necesita {tool} para analizar los vídeos.",
                                     parent=self)
                return
        self.status.set("Buscando vídeos y fechas de grabación…")
        self.persist_paths()
        self.start_task(lambda: ("analysis", analyze(roots, self.check_cancel, self.report)))

    def start_render(self):
        if not self.chapters:
            messagebox.showinfo("Capítulos", "Analiza primero las carpetas de vídeo.", parent=self)
            return
        title = self.film_title.get().strip()
        if not title or len(title) > 140:
            messagebox.showerror("Título", "Escribe un título de entre 1 y 140 caracteres.", parent=self)
            return
        try:
            minutes = int(self.minutes.get())
            if not 1 <= minutes <= 60:
                raise ValueError
        except ValueError:
            messagebox.showerror("Duración", "Elige entre 1 y 60 minutos.", parent=self)
            return
        music = Path(self.music.get().strip()).expanduser().resolve() if self.music.get().strip() else None
        if music and not music.is_dir():
            messagebox.showerror("Carpeta de música", "La carpeta seleccionada no está disponible.", parent=self)
            return
        name = filedialog.asksaveasfilename(
            parent=self, initialdir=self.output_dir.get().strip() or str(Path(self.initial_folder).parent),
            initialfile=re.sub(r'[\\/:*?"<>|]', "-", title) + ".mp4",
            defaultextension=".mp4", filetypes=[("Vídeo MP4", "*.mp4")],
        )
        if not name:
            return
        output = Path(name).expanduser().absolute()
        if output.suffix.lower() != ".mp4":
            messagebox.showerror("Formato de salida", "Usa un nombre de archivo terminado en .mp4.", parent=self)
            return
        roots = [Path(folder).resolve() for folder in self.folders.get(0, "end")]
        if output.exists() and any(output.resolve().is_relative_to(root) for root in roots):
            messagebox.showerror("Archivo original", "Elige otro nombre para evitar sustituir un vídeo de origen.",
                                 parent=self)
            return
        if not shutil.which("ffmpeg"):
            messagebox.showerror("Falta FFmpeg", "Se necesita ffmpeg para generar el recuerdo.", parent=self)
            return
        self.output_dir.set(str(output.parent))
        self.persist_paths()
        transitions = TRANSITIONS[self.transition.get()]
        title_styles = TITLE_STYLES[self.title_style.get()]
        self.status.set("Solicitando el bloqueo de suspensión…")

        def render():
            with self.inhibit():
                render_reel(self.chapters, title, minutes, transitions, title_styles, music, output,
                            lambda cmd, progress: self.encode(cmd, self.stop, progress),
                            self.check_cancel, self.report)
            return "render", output

        self.start_task(render)

    def poll(self):
        for _ in range(100):
            try:
                kind, value = self.events.get_nowait()
            except Empty:
                break
            if kind == "progress":
                if not self.stop.is_set():
                    message, percent = value
                    self.status.set(message)
                    self.progress.config(value=percent)
                continue
            self.busy = False
            if self.close_after:
                self.destroy()
                return
            for control in self.controls:
                control.state(["!disabled"])
            self.cancel_button.state(["disabled"])
            if kind == "cancelled":
                self.status.set("Tarea cancelada.")
            elif kind == "error":
                self.status.set("La tarea no pudo completarse.")
                messagebox.showerror("Crear recuerdo", value, parent=self)
            elif value[0] == "analysis":
                self.chapters, skipped, too_short, too_dark, self.rejected = value[1]
                self.tree.delete(*self.tree.get_children())
                for index, chapter in enumerate(self.chapters):
                    self.tree.insert("", "end", iid=str(index), values=(chapter.title, len(chapter.clips)))
                count = sum(len(chapter.clips) for chapter in self.chapters)
                result = f"{len(self.chapters)} capítulos · {count} vídeos. Revisa los títulos."
                if not count:
                    result = "No hay clips de al menos 3 segundos con imagen visible."
                if too_short == 1:
                    result += " Se excluyó 1 clip de menos de 3 segundos."
                elif too_short:
                    result += f" Se excluyeron {too_short} clips de menos de 3 segundos."
                if too_dark == 1:
                    result += " Se excluyó 1 clip sin imagen visible."
                elif too_dark:
                    result += f" Se excluyeron {too_dark} clips sin imagen visible."
                if skipped:
                    result += f" Se omitieron {skipped} archivos ilegibles."
                self.status.set(result)
                self.progress.config(value=100)
            else:
                self.status.set(f"Vídeo guardado en {value[1]}")
                self.progress.config(value=100)
                if messagebox.askyesno("Recuerdo listo", f"Vídeo guardado en:\n{value[1]}\n\n¿Verlo ahora?",
                                       parent=self):
                    self.master.run_player(value[1])
            self.update_rejected_button()
        self.after(100, self.poll)

    def update_rejected_button(self):
        count = len(self.rejected)
        self.rejected_button.config(text=f"Revisar descartados ({count})…" if count
                                    else "Revisar descartados…")
        self.rejected_button.state(["!disabled" if count and not self.busy else "disabled"])

    def review_rejected(self):
        if self.rejected and not self.busy:
            RejectedWindow(self)

    def cancel(self):
        self.stop.set()
        self.cancel_button.state(["disabled"])
        self.status.set("Cancelando la tarea…")

    def close(self):
        if not self.busy:
            self.destroy()
        elif self.stop.is_set() or messagebox.askyesno(
                "Cerrar", "¿Cancelar la tarea y cerrar esta ventana?", parent=self):
            self.close_after = True
            self.cancel()


class RejectedWindow(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.transient(parent)
        self.title("Revisar archivos descartados")
        self.geometry("900x420")
        self.minsize(560, 260)
        self.files = parent.rejected
        self.protocol("WM_DELETE_WINDOW", self.close)

        body = ttk.Frame(self, padding=10)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Selecciona los archivos que quieras eliminar. "
                  "Puedes elegir el DV, el MP4 o ambos.").pack(anchor="w", pady=(0, 6))
        actions = ttk.Frame(body)
        actions.pack(side="bottom", fill="x", pady=(6, 0))
        ttk.Button(actions, text="Cerrar", command=self.close).pack(side="right")
        self.delete_button = ttk.Button(actions, text="Eliminar selección…", command=self.delete)
        self.delete_button.pack(side="right", padx=6)
        self.preview_button = ttk.Button(actions, text="Ver archivo", command=self.preview)
        self.preview_button.pack(side="left")

        self.table = ttk.Treeview(body, columns=("path", "reason"), show="headings",
                                  selectmode="extended")
        self.table.heading("path", text="Archivo")
        self.table.heading("reason", text="Motivo del descarte del clip")
        self.table.column("path", width=700, stretch=False)
        self.table.column("reason", width=175, stretch=False)
        horizontal = ttk.Scrollbar(body, orient="horizontal", command=self.table.xview)
        horizontal.pack(side="bottom", fill="x")
        vertical = ttk.Scrollbar(body, orient="vertical", command=self.table.yview)
        vertical.pack(side="right", fill="y")
        self.table.configure(xscrollcommand=horizontal.set, yscrollcommand=vertical.set)
        self.table.pack(fill="both", expand=True)
        self.table.bind("<<TreeviewSelect>>", self.update_actions)
        self.rows = {}
        for index, item in enumerate(self.files.values()):
            row = str(index)
            self.rows[row] = item
            self.table.insert("", "end", iid=row, values=(str(item.path), item.reason))
        self.update_actions()
        self.wait_visibility()
        self.grab_set()

    def update_actions(self, _event=None):
        count = len(self.table.selection())
        self.delete_button.state(["!disabled" if count else "disabled"])
        self.preview_button.state(["!disabled" if count == 1 else "disabled"])

    def preview(self):
        selection = self.table.selection()
        if len(selection) == 1:
            self.master.master.run_player(self.rows[selection[0]].path)

    def delete(self):
        selected = self.table.selection()
        if not selected:
            return
        details = "\n".join(str(self.rows[row].path) for row in selected[:6])
        if len(selected) > 6:
            details += f"\n… y {len(selected) - 6} más."
        if not messagebox.askyesno(
            "Eliminar archivos definitivamente",
            f"Se eliminarán {len(selected)} archivos del disco de forma permanente.\n\n"
            f"{details}\n\n¿Confirmas el borrado?",
            icon="warning", default="no", parent=self,
        ):
            return
        errors = []
        removed = 0
        for row in selected:
            item = self.rows[row]
            try:
                if file_identity(item.path) != item.identity:
                    raise ValueError("El archivo cambió. Analiza las carpetas de nuevo.")
                item.path.unlink()
            except (OSError, ValueError) as exc:
                errors.append(f"{item.path.name}: {exc}")
                continue
            self.files.pop(item.path)
            del self.rows[row]
            self.table.delete(row)
            removed += 1
        if removed:
            self.master.update_rejected_button()
            self.master.status.set(f"Archivos eliminados: {removed}. Pendientes de revisar: {len(self.files)}.")
            self.master.master.refresh_file_list()
        if errors:
            messagebox.showerror("Algunos archivos no se pudieron eliminar", "\n".join(errors[:8]),
                                 parent=self)
        self.update_actions()

    def close(self):
        parent = self.master
        self.destroy()
        parent.grab_set()
