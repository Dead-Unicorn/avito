from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests
from PIL import Image, ImageOps
import tkinter as tk
from tkinter import filedialog, messagebox, ttk, colorchooser


API_BASE = "https://cloud-api.yandex.net/v1/disk"
DEFAULT_TIMEOUT = 60
UPLOAD_TIMEOUT = 300
APP_TITLE = "WEBP → JPG + Яндекс Диск"
CONFIG_PATH = Path.home() / ".webp_to_jpg_yadisk_gui.json"


@dataclass
class AppSettings:
    source_dir: str = ""
    output_dir: str = ""
    token: str = ""
    yadisk_folder: str = ""
    quality: int = 95
    recursive: bool = False
    overwrite: bool = False
    bg_color: str = "255,255,255"
    save_token: bool = False


class WorkerSignals:
    LOG = "log"
    STATUS = "status"
    PROGRESS_MAX = "progress_max"
    PROGRESS_STEP = "progress_step"
    DONE = "done"
    ERROR = "error"
    ENABLE = "enable"
    LINKS = "links"


class YaDiskClient:
    def __init__(self, token: str):
        self.token = token.strip()
        if not self.token:
            raise ValueError("Не указан OAuth-токен Яндекс Диска")

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"OAuth {self.token}"}

    def _safe_raise(self, response: requests.Response, context: str) -> None:
        if response.ok:
            return
        try:
            details = response.json()
        except Exception:
            details = response.text

        if response.status_code == 401:
            raise RuntimeError(
                "Ошибка авторизации Яндекс Диска (401). Проверь OAuth-токен. "
                "Если используешь debug token, возможно, он истёк или был отозван. "
                f"Контекст: {context}"
            )

        raise RuntimeError(f"{context}: HTTP {response.status_code} -> {details}")

    def normalize_path(self, path: str) -> str:
        path = path.strip()
        if not path:
            raise ValueError("Путь на Яндекс Диске не должен быть пустым")
        if path.startswith("disk:/"):
            normalized = path
        else:
            normalized = f"disk:/{path.lstrip('/')}"
        while "//" in normalized:
            normalized = normalized.replace("//", "/")
        return normalized.rstrip("/")

    def resource_exists(self, remote_path: str) -> bool:
        remote_path = self.normalize_path(remote_path)
        response = requests.get(
            f"{API_BASE}/resources",
            headers=self.headers,
            params={"path": remote_path},
            timeout=DEFAULT_TIMEOUT,
        )
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        self._safe_raise(response, f"Не удалось проверить существование ресурса {remote_path}")
        return False

    def ensure_folder(self, folder_path: str) -> None:
        folder_path = self.normalize_path(folder_path)
        relative = folder_path.removeprefix("disk:/").strip("/")
        if not relative:
            return

        current = "disk:"
        for part in relative.split("/"):
            current = f"{current}/{part}" if current != "disk:" else f"disk:/{part}"
            response = requests.put(
                f"{API_BASE}/resources",
                headers=self.headers,
                params={"path": current},
                timeout=DEFAULT_TIMEOUT,
            )
            if response.status_code in (201, 409):
                continue
            self._safe_raise(response, f"Не удалось создать папку {current}")

    def get_upload_link(self, remote_file_path: str, overwrite: bool) -> tuple[str, str]:
        response = requests.get(
            f"{API_BASE}/resources/upload",
            headers=self.headers,
            params={"path": remote_file_path, "overwrite": str(overwrite).lower()},
            timeout=DEFAULT_TIMEOUT,
        )
        self._safe_raise(response, f"Не удалось получить ссылку загрузки для {remote_file_path}")
        data = response.json()
        href = data.get("href")
        method = data.get("method", "PUT").upper()
        if not href:
            raise RuntimeError(f"В ответе нет href для {remote_file_path}")
        return href, method

    def get_resource_meta(self, remote_path: str, fields: str | None = None) -> dict:
        remote_path = self.normalize_path(remote_path)
        params = {"path": remote_path}
        if fields:
            params["fields"] = fields
        response = requests.get(
            f"{API_BASE}/resources",
            headers=self.headers,
            params=params,
            timeout=DEFAULT_TIMEOUT,
        )
        self._safe_raise(response, f"Не удалось получить метаданные ресурса {remote_path}")
        return response.json()

    def publish_resource(self, remote_path: str) -> None:
        remote_path = self.normalize_path(remote_path)
        response = requests.put(
            f"{API_BASE}/resources/publish",
            headers=self.headers,
            params={"path": remote_path},
            timeout=DEFAULT_TIMEOUT,
        )
        if response.status_code not in (200, 201, 202):
            self._safe_raise(response, f"Не удалось опубликовать ресурс {remote_path}")

    @staticmethod
    def convert_public_url_for_jpg(public_url: str) -> str:
        url = public_url.strip()
        if not url:
            return url
        url = url.replace("https://yadi.sk/", "https://disk.yandex.ru/")
        if not url.lower().endswith(".jpg"):
            url = f"{url}.jpg"
        return url

    def get_public_jpg_url(self, remote_path: str) -> str:
        import time

        remote_path = self.normalize_path(remote_path)
        self.publish_resource(remote_path)

        last_error = None
        for _ in range(12):
            try:
                meta = self.get_resource_meta(remote_path, fields="public_url")
                public_url = (meta or {}).get("public_url")
                if public_url:
                    return self.convert_public_url_for_jpg(public_url)
            except Exception as e:
                last_error = e
            time.sleep(0.5)

        if last_error:
            raise RuntimeError(f"Не удалось получить публичную ссылку для {remote_path}: {last_error}")
        raise RuntimeError(f"Не удалось получить публичную ссылку для {remote_path}")

    def upload_file(self, local_file: Path, remote_file_path: str, overwrite: bool) -> bool:
        remote_file_path = self.normalize_path(remote_file_path)

        if not overwrite and self.resource_exists(remote_file_path):
            return False

        href, method = self.get_upload_link(remote_file_path, overwrite)
        with local_file.open("rb") as f:
            response = requests.request(
                method=method,
                url=href,
                data=f,
                headers={"Content-Type": "application/octet-stream"},
                timeout=UPLOAD_TIMEOUT,
            )
        if response.status_code not in (201, 202):
            self._safe_raise(response, f"Не удалось загрузить {remote_file_path}")
        return True


class ConverterUploader:
    def __init__(self, settings: AppSettings, signal_queue: queue.Queue):
        self.settings = settings
        self.q = signal_queue
        self.client = YaDiskClient(settings.token)

    def emit(self, event: str, value=None) -> None:
        self.q.put((event, value))

    def log(self, text: str) -> None:
        self.emit(WorkerSignals.LOG, text)

    def status(self, text: str) -> None:
        self.emit(WorkerSignals.STATUS, text)

    @staticmethod
    def parse_bg_color(value: str) -> tuple[int, int, int]:
        parts = [int(x.strip()) for x in value.split(",")]
        if len(parts) != 3 or any(not (0 <= x <= 255) for x in parts):
            raise ValueError("Цвет фона должен быть в формате R,G,B, например 255,255,255")
        return tuple(parts)  # type: ignore[return-value]

    @staticmethod
    def iter_webp_files(source_dir: Path, recursive: bool) -> Iterable[Path]:
        if recursive:
            yield from sorted(p for p in source_dir.rglob("*") if p.is_file() and p.suffix.lower() == ".webp")
        else:
            yield from sorted(p for p in source_dir.iterdir() if p.is_file() and p.suffix.lower() == ".webp")

    @staticmethod
    def convert_one(src: Path, dst: Path, quality: int, bg_color: tuple[int, int, int]) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as img:
            img = ImageOps.exif_transpose(img)
            icc_profile = img.info.get("icc_profile")
            exif = img.info.get("exif")
            has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)

            if has_alpha:
                rgba = img.convert("RGBA")
                background = Image.new("RGBA", rgba.size, bg_color + (255,))
                img = Image.alpha_composite(background, rgba).convert("RGB")
            else:
                img = img.convert("RGB")

            save_kwargs = {
                "format": "JPEG",
                "quality": quality,
                "subsampling": 0,
                "optimize": True,
            }
            if icc_profile:
                save_kwargs["icc_profile"] = icc_profile
            if exif:
                save_kwargs["exif"] = exif

            img.save(dst, **save_kwargs)

    def run(self) -> None:
        try:
            source_dir = Path(self.settings.source_dir).expanduser().resolve()
            if not source_dir.exists() or not source_dir.is_dir():
                raise ValueError("Исходная папка не найдена")

            output_dir = Path(self.settings.output_dir).expanduser().resolve() if self.settings.output_dir else source_dir / "jpg_converted"
            remote_root = self.client.normalize_path(self.settings.yadisk_folder)
            bg_color = self.parse_bg_color(self.settings.bg_color)

            if not (1 <= int(self.settings.quality) <= 100):
                raise ValueError("Качество JPEG должно быть в диапазоне 1..100")

            files = list(self.iter_webp_files(source_dir, self.settings.recursive))
            if not files:
                raise ValueError("В выбранной папке не найдено ни одного .webp файла")

            self.client.ensure_folder(remote_root)

            self.emit(WorkerSignals.PROGRESS_MAX, len(files) * 3)
            self.log(f"Найдено WEBP-файлов: {len(files)}")
            self.log(f"Локальная папка JPG: {output_dir}")
            self.log(f"Папка на Яндекс Диске: {remote_root}")

            converted: list[Path] = []
            for src in files:
                rel = src.relative_to(source_dir)
                dst = (output_dir / rel).with_suffix(".jpg")
                self.status(f"Конвертация: {src.name}")
                self.log(f"[CONVERT] {src} -> {dst}")
                self.convert_one(src, dst, int(self.settings.quality), bg_color)
                converted.append(dst)
                self.emit(WorkerSignals.PROGRESS_STEP, 1)

            self.log("Конвертация завершена.")

            uploaded_count = 0
            skipped_count = 0
            uploaded_links: list[str] = []
            links_failed_count = 0

            for jpg_file in converted:
                rel = jpg_file.relative_to(output_dir)
                remote_file = f"{remote_root}/{rel.as_posix()}"

                parent_rel = rel.parent.as_posix()
                if parent_rel != ".":
                    self.client.ensure_folder(f"{remote_root}/{parent_rel}")

                self.status(f"Загрузка: {jpg_file.name}")
                self.log(f"[UPLOAD] {jpg_file} -> {remote_file}")
                uploaded = self.client.upload_file(jpg_file, remote_file, self.settings.overwrite)
                if uploaded:
                    uploaded_count += 1
                    try:
                        self.status(f"Получение ссылки: {jpg_file.name}")
                        public_link = self.client.get_public_jpg_url(remote_file)
                        uploaded_links.append(public_link)
                        self.log(f"[LINK] {public_link}")
                    except Exception as link_error:
                        links_failed_count += 1
                        self.log(f"[WARN] Не удалось получить ссылку для {remote_file}: {link_error}")
                else:
                    skipped_count += 1
                    self.log(f"[SKIP] Файл уже существует на Яндекс Диске: {remote_file}")
                self.emit(WorkerSignals.PROGRESS_STEP, 1)

            links_text = " | ".join(uploaded_links)
            self.emit(WorkerSignals.LINKS, links_text)
            if links_text:
                self.log(f"Итоговые ссылки: {links_text}")
            elif uploaded_count > 0:
                self.log("[WARN] Файлы загружены, но ссылки получить не удалось.")

            self.status("Готово")
            if skipped_count:
                self.log(
                    f"Готово. Загружено: {uploaded_count}, пропущено из-за совпадающих имён: {skipped_count}, ссылок получено: {len(uploaded_links)}."
                )
            else:
                self.log(
                    f"Все JPG успешно загружены на Яндекс Диск. Загружено: {uploaded_count}, ссылок получено: {len(uploaded_links)}."
                )
            if links_failed_count:
                self.log(f"Не удалось получить ссылку для {links_failed_count} файл(ов).")
            self.emit(WorkerSignals.DONE, None)
        except Exception as e:
            self.emit(WorkerSignals.ERROR, str(e))
        finally:
            self.emit(WorkerSignals.ENABLE, True)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("950x760")
        self.minsize(860, 660)

        self.signal_queue: queue.Queue = queue.Queue()
        self.worker_thread: threading.Thread | None = None

        self.settings = self.load_settings()
        self._build_ui()
        self._bind_shortcuts()
        self._apply_settings()
        self.after(100, self._poll_queue)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        outer = ttk.Frame(self, padding=16)
        outer.grid(sticky="nsew")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(6, weight=1)

        title = ttk.Label(outer, text=APP_TITLE, font=("Segoe UI", 16, "bold"))
        title.grid(row=0, column=0, sticky="w")

        subtitle = ttk.Label(
            outer,
            text="Конвертация всех WEBP в JPG и загрузка результата в папку на Яндекс Диске",
        )
        subtitle.grid(row=1, column=0, sticky="w", pady=(4, 14))

        form = ttk.LabelFrame(outer, text="Настройки", padding=12)
        form.grid(row=2, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)

        self.source_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.token_var = tk.StringVar()
        self.yadisk_var = tk.StringVar()
        self.quality_var = tk.IntVar(value=95)
        self.recursive_var = tk.BooleanVar(value=False)
        self.overwrite_var = tk.BooleanVar(value=False)
        self.bg_color_var = tk.StringVar(value="255,255,255")
        self.save_token_var = tk.BooleanVar(value=False)
        self.show_token_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Готов к работе")
        self.links_var = tk.StringVar(value="")

        row = 0
        ttk.Label(form, text="Папка с WEBP:").grid(row=row, column=0, sticky="w", pady=6)
        self.source_entry = ttk.Entry(form, textvariable=self.source_var)
        self.source_entry.grid(row=row, column=1, sticky="ew", pady=6)
        ttk.Button(form, text="Выбрать", command=self._choose_source).grid(row=row, column=2, sticky="e", padx=(8, 0), pady=6)

        row += 1
        ttk.Label(form, text="Папка для JPG:").grid(row=row, column=0, sticky="w", pady=6)
        self.output_entry = ttk.Entry(form, textvariable=self.output_var)
        self.output_entry.grid(row=row, column=1, sticky="ew", pady=6)
        ttk.Button(form, text="Выбрать", command=self._choose_output).grid(row=row, column=2, sticky="e", padx=(8, 0), pady=6)

        row += 1
        ttk.Label(form, text="Папка на Яндекс Диске:").grid(row=row, column=0, sticky="w", pady=6)
        self.yadisk_entry = ttk.Entry(form, textvariable=self.yadisk_var)
        self.yadisk_entry.grid(row=row, column=1, sticky="ew", pady=6)
        ttk.Label(form, text='Например: Фото/Новая папка').grid(row=row, column=2, sticky="w", padx=(8, 0), pady=6)

        row += 1
        ttk.Label(form, text="OAuth-токен:").grid(row=row, column=0, sticky="w", pady=6)
        self.token_entry = ttk.Entry(form, textvariable=self.token_var, show="•")
        self.token_entry.grid(row=row, column=1, sticky="ew", pady=6)

        token_box = ttk.Frame(form)
        token_box.grid(row=row, column=2, sticky="w", padx=(8, 0), pady=6)
        ttk.Checkbutton(token_box, text="Показать", variable=self.show_token_var, command=self._toggle_token_visibility).pack(side="left")
        ttk.Checkbutton(token_box, text="Сохранить токен", variable=self.save_token_var).pack(side="left", padx=(8, 0))

        ttk.Label(form, text="Вставка работает через Ctrl+V, Shift+Insert и меню правой кнопкой мыши.").grid(row=row, column=2, sticky="w", padx=(8, 0), pady=(36, 0))

        row += 1
        options = ttk.Frame(form)
        options.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        for i in range(6):
            options.columnconfigure(i, weight=1 if i in (1, 3) else 0)

        ttk.Label(options, text="Качество JPG:").grid(row=0, column=0, sticky="w")
        self.quality_spin = ttk.Spinbox(options, from_=1, to=100, textvariable=self.quality_var, width=8)
        self.quality_spin.grid(row=0, column=1, sticky="w", padx=(8, 20))

        ttk.Label(options, text="Цвет фона:").grid(row=0, column=2, sticky="w")
        self.bg_entry = ttk.Entry(options, textvariable=self.bg_color_var, width=16)
        self.bg_entry.grid(row=0, column=3, sticky="w", padx=(8, 8))
        ttk.Button(options, text="Выбрать цвет", command=self._choose_color).grid(row=0, column=4, sticky="w")

        row += 1
        checks = ttk.Frame(form)
        checks.grid(row=row, column=0, columnspan=3, sticky="w", pady=(12, 0))
        self.recursive_check = ttk.Checkbutton(checks, text="Искать WEBP во всех подпапках", variable=self.recursive_var)
        self.recursive_check.pack(side="left")
        self.overwrite_check = ttk.Checkbutton(checks, text="Перезаписывать файлы на Яндекс Диске", variable=self.overwrite_var)
        self.overwrite_check.pack(side="left", padx=(16, 0))

        controls = ttk.Frame(outer)
        controls.grid(row=3, column=0, sticky="ew", pady=(14, 10))
        controls.columnconfigure(4, weight=1)

        self.start_button = ttk.Button(controls, text="Начать", command=self._start)
        self.start_button.grid(row=0, column=0, sticky="w")
        self.clear_button = ttk.Button(controls, text="Очистить лог", command=self._clear_log)
        self.clear_button.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.copy_log_button = ttk.Button(controls, text="Скопировать лог", command=self._copy_log)
        self.copy_log_button.grid(row=0, column=2, sticky="w", padx=(8, 0))
        self.save_button = ttk.Button(controls, text="Сохранить настройки", command=self._save_settings_manual)
        self.save_button.grid(row=0, column=3, sticky="w", padx=(8, 0))

        self.progress = ttk.Progressbar(controls, mode="determinate")
        self.progress.grid(row=0, column=4, sticky="ew", padx=(16, 0))

        status_frame = ttk.Frame(outer)
        status_frame.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        status_frame.columnconfigure(0, weight=1)
        ttk.Label(status_frame, textvariable=self.status_var).grid(row=0, column=0, sticky="w")

        links_frame = ttk.LabelFrame(outer, text="Ссылки на только что загруженные JPG", padding=10)
        links_frame.grid(row=5, column=0, sticky="ew", pady=(0, 10))
        links_frame.columnconfigure(0, weight=1)
        self.links_entry = ttk.Entry(links_frame, textvariable=self.links_var)
        self.links_entry.grid(row=0, column=0, sticky="ew")
        ttk.Button(links_frame, text="Скопировать", command=self._copy_links).grid(row=0, column=1, sticky="e", padx=(8, 0))

        log_frame = ttk.LabelFrame(outer, text="Журнал", padding=10)
        log_frame.grid(row=6, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(log_frame, wrap="word", height=18)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)

        note = ttk.Label(
            outer,
            text=(
                "Подсказка: если поле 'Папка для JPG' оставить пустым, будет создана подпапка jpg_converted внутри исходной папки."
            ),
        )
        note.grid(row=7, column=0, sticky="w", pady=(10, 0))

        self._set_log_readonly(True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _apply_settings(self) -> None:
        self.source_var.set(self.settings.source_dir)
        self.output_var.set(self.settings.output_dir)
        self.token_var.set(self.settings.token)
        self.yadisk_var.set(self.settings.yadisk_folder)
        self.quality_var.set(self.settings.quality)
        self.recursive_var.set(self.settings.recursive)
        self.overwrite_var.set(self.settings.overwrite)
        self.bg_color_var.set(self.settings.bg_color)
        self.save_token_var.set(self.settings.save_token)

    def _set_log_readonly(self, readonly: bool) -> None:
        self.log_text.configure(state="disabled" if readonly else "normal")

    def _bind_shortcuts(self) -> None:
        self.context_menu = tk.Menu(self, tearoff=0)
        self.context_menu.add_command(label="Вырезать", command=lambda: self._context_action("cut"))
        self.context_menu.add_command(label="Копировать", command=lambda: self._context_action("copy"))
        self.context_menu.add_command(label="Вставить", command=lambda: self._context_action("paste"))
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Выделить всё", command=lambda: self._context_action("select_all"))

        widgets = [
            self.source_entry,
            self.output_entry,
            self.yadisk_entry,
            self.token_entry,
            self.quality_spin,
            self.bg_entry,
            self.links_entry,
            self.log_text,
        ]

        paste_sequences = (
            "<<Paste>>",
            "<Control-v>",
            "<Control-V>",
            "<Command-v>",
            "<Command-V>",
            "<Shift-Insert>",
        )
        copy_sequences = (
            "<<Copy>>",
            "<Control-c>",
            "<Control-C>",
            "<Command-c>",
            "<Command-C>",
            "<Control-Insert>",
        )
        cut_sequences = (
            "<<Cut>>",
            "<Control-x>",
            "<Control-X>",
            "<Command-x>",
            "<Command-X>",
            "<Shift-Delete>",
        )
        select_all_sequences = (
            "<Control-a>",
            "<Control-A>",
            "<Command-a>",
            "<Command-A>",
        )

        for widget in widgets:
            for seq in paste_sequences:
                widget.bind(seq, self._handle_paste, add="+")
            for seq in copy_sequences:
                widget.bind(seq, self._handle_copy, add="+")
            for seq in cut_sequences:
                widget.bind(seq, self._handle_cut, add="+")
            for seq in select_all_sequences:
                widget.bind(seq, self._handle_select_all, add="+")
            for seq in ("<Button-3>", "<Button-2>"):
                widget.bind(seq, self._show_context_menu, add="+")

    def _focused_widget(self):
        return self.focus_get()

    def _event_widget(self, event=None):
        widget = getattr(event, "widget", None)
        return widget or self._focused_widget()

    @staticmethod
    def _is_entry_like(widget) -> bool:
        return hasattr(widget, "get") and hasattr(widget, "insert") and hasattr(widget, "delete") and hasattr(widget, "icursor")

    @staticmethod
    def _is_text_like(widget) -> bool:
        return isinstance(widget, tk.Text)

    def _get_clipboard_text(self) -> str:
        try:
            return self.clipboard_get()
        except tk.TclError:
            return ""

    def _show_context_menu(self, event):
        widget = self._event_widget(event)
        if widget is not None:
            try:
                widget.focus_force()
            except Exception:
                pass
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()
        return "break"

    def _context_action(self, action: str) -> None:
        if action == "paste":
            self._handle_paste()
        elif action == "copy":
            self._handle_copy()
        elif action == "cut":
            self._handle_cut()
        elif action == "select_all":
            self._handle_select_all()

    def _handle_paste(self, event=None):
        widget = self._event_widget(event)
        text = self._get_clipboard_text()
        if not text or widget is None:
            return None

        if self._is_entry_like(widget):
            try:
                if str(widget.cget("state")) in ("disabled", "readonly"):
                    return "break"
            except Exception:
                pass
            try:
                widget.delete("sel.first", "sel.last")
            except Exception:
                pass
            try:
                widget.insert("insert", text)
                return "break"
            except Exception:
                return None

        if self._is_text_like(widget):
            if str(widget.cget("state")) == "disabled":
                return "break"
            try:
                widget.delete("sel.first", "sel.last")
            except Exception:
                pass
            widget.insert("insert", text)
            return "break"
        return None

    def _handle_copy(self, event=None):
        widget = self._event_widget(event)
        if widget is None:
            return None

        selected = ""
        if self._is_entry_like(widget):
            try:
                selected = widget.selection_get()
            except Exception:
                try:
                    selected = widget.get()
                except Exception:
                    selected = ""
        elif self._is_text_like(widget):
            try:
                selected = widget.get("sel.first", "sel.last")
            except Exception:
                selected = widget.get("1.0", "end-1c")

        if selected:
            self.clipboard_clear()
            self.clipboard_append(selected)
            self.update_idletasks()
            return "break"
        return None

    def _handle_cut(self, event=None):
        widget = self._event_widget(event)
        copy_result = self._handle_copy(event)
        if widget is None or copy_result is None:
            return None

        if self._is_entry_like(widget):
            try:
                if str(widget.cget("state")) in ("disabled", "readonly"):
                    return "break"
            except Exception:
                pass
            try:
                widget.delete("sel.first", "sel.last")
            except Exception:
                pass
            return "break"

        if self._is_text_like(widget):
            if str(widget.cget("state")) == "disabled":
                return "break"
            try:
                widget.delete("sel.first", "sel.last")
            except Exception:
                pass
            return "break"
        return None

    def _handle_select_all(self, event=None):
        widget = self._event_widget(event)
        if widget is None:
            return None

        if self._is_entry_like(widget):
            try:
                widget.selection_range(0, "end")
                widget.icursor("end")
                return "break"
            except Exception:
                return None

        if self._is_text_like(widget):
            widget.tag_add("sel", "1.0", "end-1c")
            widget.mark_set("insert", "1.0")
            widget.see("insert")
            return "break"
        return None

    def _toggle_token_visibility(self) -> None:
        self.token_entry.configure(show="" if self.show_token_var.get() else "•")

    def _choose_source(self) -> None:
        path = filedialog.askdirectory(title="Выберите папку с WEBP")
        if path:
            self.source_var.set(path)
            if not self.output_var.get().strip():
                self.output_var.set(str(Path(path) / "jpg_converted"))

    def _choose_output(self) -> None:
        path = filedialog.askdirectory(title="Выберите папку для JPG")
        if path:
            self.output_var.set(path)

    def _choose_color(self) -> None:
        result = colorchooser.askcolor(title="Выберите цвет фона")
        if result and result[0]:
            r, g, b = (int(v) for v in result[0])
            self.bg_color_var.set(f"{r},{g},{b}")

    def _append_log(self, text: str) -> None:
        self._set_log_readonly(False)
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self._set_log_readonly(True)

    def _clear_log(self) -> None:
        self._set_log_readonly(False)
        self.log_text.delete("1.0", "end")
        self._set_log_readonly(True)

    def _copy_log(self) -> None:
        text = self.log_text.get("1.0", "end-1c").strip()
        if not text:
            messagebox.showinfo("Лог пуст", "Пока нечего копировать.")
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update_idletasks()
        messagebox.showinfo("Готово", "Журнал скопирован в буфер обмена.")

    def _copy_links(self) -> None:
        links = self.links_var.get().strip()
        if not links:
            messagebox.showinfo("Ссылок нет", "Пока нет ссылок для копирования.")
            return
        self.clipboard_clear()
        self.clipboard_append(links)
        self.update_idletasks()
        messagebox.showinfo("Готово", "Ссылки скопированы в буфер обмена.")

    def _gather_settings(self) -> AppSettings:
        token = self.token_var.get().strip()
        save_token = self.save_token_var.get()
        return AppSettings(
            source_dir=self.source_var.get().strip(),
            output_dir=self.output_var.get().strip(),
            token=token,
            yadisk_folder=self.yadisk_var.get().strip(),
            quality=int(self.quality_var.get()),
            recursive=self.recursive_var.get(),
            overwrite=self.overwrite_var.get(),
            bg_color=self.bg_color_var.get().strip(),
            save_token=save_token,
        )

    def _validate(self, settings: AppSettings) -> None:
        if not settings.source_dir:
            raise ValueError("Выбери папку с WEBP")
        if not settings.yadisk_folder:
            raise ValueError("Укажи папку на Яндекс Диске")
        if not settings.token:
            raise ValueError("Вставь OAuth-токен")
        source = Path(settings.source_dir)
        if not source.exists() or not source.is_dir():
            raise ValueError("Папка с WEBP не найдена")

    def _set_controls_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for widget in [
            self.source_entry,
            self.output_entry,
            self.yadisk_entry,
            self.token_entry,
            self.quality_spin,
            self.bg_entry,
            self.start_button,
            self.clear_button,
            self.copy_log_button,
            self.save_button,
            self.recursive_check,
            self.overwrite_check,
            self.links_entry,
        ]:
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass

    def _start(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            return
        try:
            settings = self._gather_settings()
            self._validate(settings)
            self.save_settings(settings)

            self.progress["value"] = 0
            self.progress["maximum"] = 1
            self.links_var.set("")
            self.status_var.set("Подготовка...")
            self._append_log("Старт задачи...")
            self._set_controls_enabled(False)

            worker = ConverterUploader(settings, self.signal_queue)
            self.worker_thread = threading.Thread(target=worker.run, daemon=True)
            self.worker_thread.start()
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))

    def _poll_queue(self) -> None:
        while True:
            try:
                event, value = self.signal_queue.get_nowait()
            except queue.Empty:
                break

            if event == WorkerSignals.LOG:
                self._append_log(str(value))
            elif event == WorkerSignals.STATUS:
                self.status_var.set(str(value))
            elif event == WorkerSignals.PROGRESS_MAX:
                self.progress["maximum"] = max(1, int(value))
                self.progress["value"] = 0
            elif event == WorkerSignals.PROGRESS_STEP:
                self.progress["value"] = float(self.progress["value"]) + float(value)
            elif event == WorkerSignals.DONE:
                if self.links_var.get().strip():
                    messagebox.showinfo("Готово", "Обработка завершена. Ссылки уже выведены в отдельное поле и журнал.")
                else:
                    messagebox.showinfo("Готово", "Обработка завершена. Ссылки не были получены — смотри предупреждения в журнале.")
            elif event == WorkerSignals.LINKS:
                self.links_var.set(str(value or ""))
            elif event == WorkerSignals.ERROR:
                self.status_var.set("Ошибка")
                self._append_log(f"Ошибка: {value}")
                messagebox.showerror("Ошибка", str(value))
            elif event == WorkerSignals.ENABLE:
                self._set_controls_enabled(bool(value))

        self.after(100, self._poll_queue)

    def load_settings(self) -> AppSettings:
        try:
            if not CONFIG_PATH.exists():
                return AppSettings()
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            settings = AppSettings(**data)
            if not settings.save_token:
                settings.token = ""
            return settings
        except Exception:
            return AppSettings()

    def save_settings(self, settings: AppSettings) -> None:
        data = {
            "source_dir": settings.source_dir,
            "output_dir": settings.output_dir,
            "token": settings.token if settings.save_token else "",
            "yadisk_folder": settings.yadisk_folder,
            "quality": settings.quality,
            "recursive": settings.recursive,
            "overwrite": settings.overwrite,
            "bg_color": settings.bg_color,
            "save_token": settings.save_token,
        }
        CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _save_settings_manual(self) -> None:
        try:
            settings = self._gather_settings()
            self.save_settings(settings)
            messagebox.showinfo("Готово", "Настройки сохранены.")
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))

    def _on_close(self) -> None:
        try:
            self.save_settings(self._gather_settings())
        except Exception:
            pass
        self.destroy()


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()