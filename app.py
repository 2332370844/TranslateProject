import html
import ctypes
import json
import os
import re
import shutil
import subprocess
import threading
import tkinter as tk
from tkinter import font
from tkinter import ttk
from urllib import error, parse, request

try:
    from PIL import ImageFilter, ImageGrab, ImageOps
except ImportError:
    ImageFilter = None
    ImageGrab = None
    ImageOps = None

try:
    import pytesseract
except ImportError:
    pytesseract = None


APP_NAME = "FanYI"
GOOGLE_TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
MYMEMORY_TRANSLATE_URL = "https://api.mymemory.translated.net/get"
IDLE_COLLAPSE_MS = 30_000
FLOATING_BUTTON_WIDTH = 68
FLOATING_BUTTON_HEIGHT = 56
OCR_LANGUAGE = "chi_sim+eng"
REQUIRED_OCR_LANGUAGES = {"chi_sim", "eng"}
MIN_CAPTURE_SIZE = 8
RESULT_WINDOW_WIDTH = 460
RESULT_WINDOW_HEIGHT = 360
COMMON_TESSERACT_PATHS = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Tesseract-OCR", "tesseract.exe"),
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]
USER_TESSDATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Tesseract-OCR", "tessdata")


LANGUAGE_CHOICES = {
    "自动：中文 ↔ 英文": ("auto", "auto"),
    "英文 → 中文": ("en", "zh-CN"),
    "中文 → 英文": ("zh-CN", "en"),
    "任意语言 → 中文": ("auto", "zh-CN"),
}


def format_geometry(width, height, x, y):
    return f"{width}x{height}{x:+d}{y:+d}"


def get_virtual_screen_bounds(root):
    try:
        user32 = ctypes.windll.user32
        left = user32.GetSystemMetrics(76)
        top = user32.GetSystemMetrics(77)
        width = user32.GetSystemMetrics(78)
        height = user32.GetSystemMetrics(79)
        if width > 0 and height > 0:
            return left, top, width, height
    except Exception:
        pass

    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def find_tesseract_command():
    env_command = os.environ.get("TESSERACT_CMD")
    candidates = [env_command, shutil.which("tesseract"), *COMMON_TESSERACT_PATHS]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def get_tesseract_languages(command):
    configure_tessdata_prefix(command)
    result = subprocess.run(
        [command, "--list-langs"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "无法读取 Tesseract 语言包。").strip())

    languages = set()
    for line in result.stdout.splitlines():
        value = line.strip()
        if value and not value.lower().startswith("list of available languages"):
            languages.add(value)
    return languages


def configure_tessdata_prefix(command):
    if os.path.isdir(USER_TESSDATA_DIR):
        os.environ["TESSDATA_PREFIX"] = USER_TESSDATA_DIR
        return

    install_dir = os.path.dirname(command)
    install_tessdata = os.path.join(install_dir, "tessdata")
    if os.path.isdir(install_tessdata):
        os.environ.setdefault("TESSDATA_PREFIX", install_tessdata)


def ensure_ocr_ready():
    if ImageGrab is None or ImageOps is None or ImageFilter is None:
        raise RuntimeError("缺少 Pillow。请运行：python -m pip install -r requirements.txt")
    if pytesseract is None:
        raise RuntimeError("缺少 pytesseract。请运行：python -m pip install -r requirements.txt")

    command = find_tesseract_command()
    if not command:
        raise RuntimeError("未找到 tesseract.exe。请安装：winget install --id tesseract-ocr.tesseract -e")

    pytesseract.pytesseract.tesseract_cmd = command
    languages = get_tesseract_languages(command)
    missing = sorted(REQUIRED_OCR_LANGUAGES - languages)
    if missing:
        raise RuntimeError(f"Tesseract 缺少语言包：{', '.join(missing)}。请安装中文和英文 OCR 语言包。")

    return command


def capture_screen_area(box):
    if ImageGrab is None:
        raise RuntimeError("缺少 Pillow，无法截图。")

    try:
        return ImageGrab.grab(bbox=box, all_screens=True)
    except TypeError:
        return ImageGrab.grab(bbox=box)


def prepare_ocr_image(image):
    grayscale = ImageOps.grayscale(image)
    enhanced = ImageOps.autocontrast(grayscale)
    width, height = enhanced.size
    scaled = enhanced.resize((width * 2, height * 2))
    return scaled.filter(ImageFilter.SHARPEN)


def resize_for_ocr(image, scale):
    width, height = image.size
    return image.resize((max(1, width * scale), max(1, height * scale)))


def crop_red_annotation_border(image):
    rgb = image.convert("RGB")
    width, height = rgb.size
    pixels = rgb.get_flattened_data() if hasattr(rgb, "get_flattened_data") else rgb.getdata()

    min_x = width
    min_y = height
    max_x = -1
    max_y = -1
    red_count = 0
    for index, (red, green, blue) in enumerate(pixels):
        if red >= 200 and green <= 90 and blue <= 90 and red - green >= 90 and red - blue >= 90:
            x = index % width
            y = index // width
            red_count += 1
            min_x = min(min_x, x)
            min_y = min(min_y, y)
            max_x = max(max_x, x)
            max_y = max(max_y, y)

    if red_count < width * height * 0.04:
        return None

    reaches_edges = (
        min_x <= width * 0.08
        and min_y <= height * 0.12
        and max_x >= width * 0.90
        and max_y >= height * 0.75
    )
    if not reaches_edges:
        return None

    inset = max(6, min(width, height) // 25)
    crop_box = (
        max(0, min_x + inset),
        max(0, min_y + inset),
        min(width, max_x - inset),
        min(height, max_y - inset),
    )
    if crop_box[2] - crop_box[0] < MIN_CAPTURE_SIZE or crop_box[3] - crop_box[1] < MIN_CAPTURE_SIZE:
        return None

    return image.crop(crop_box)


def create_ocr_variants(image):
    images = [image]
    red_border_crop = crop_red_annotation_border(image)
    if red_border_crop is not None:
        images.insert(0, red_border_crop)

    variants = []
    for source in images:
        grayscale = ImageOps.grayscale(source)
        enhanced = ImageOps.autocontrast(grayscale)
        large = resize_for_ocr(enhanced, 4).filter(ImageFilter.SHARPEN)
        inverted = ImageOps.invert(large)
        binary = resize_for_ocr(enhanced, 5).point(lambda value: 255 if value > 145 else 0)

        variants.extend(
            [
                prepare_ocr_image(source),
                large,
                inverted,
                binary,
            ]
        )

    return variants


def normalize_ocr_text(text):
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def clean_ocr_candidate(text):
    normalized = normalize_ocr_text(text)
    if not normalized:
        return ""

    has_latin = bool(re.search(r"[A-Za-z]", normalized))
    if not has_latin:
        return normalized

    cleaned_lines = []
    for line in normalized.splitlines():
        line = re.sub(r"[\u0400-\u052f]+", " ", line).strip()
        if not line:
            continue

        if re.search(r"[A-Za-z]", line):
            first_latin = re.search(r"[A-Za-z]", line)
            prefix = line[: first_latin.start()]
            if not re.search(r"[\u4e00-\u9fff]{2,}", prefix):
                line = line[first_latin.start() :]

            line = re.sub(r"^[^\w\u4e00-\u9fff]+", "", line).strip()
            line = re.sub(r"^[\u4e00-\u9fff]\s*(?=[A-Za-z])", "", line).strip()
            line = re.sub(r"^[A-Z](?=[A-Z][a-z]{2,})", "", line).strip()
        elif len(line) <= 3 and len(set(line)) <= 1:
            continue

        if re.search(r"[A-Za-z0-9\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", line):
            cleaned_lines.append(line)

    return "\n".join(cleaned_lines).strip()


def score_ocr_candidate(text):
    if not text:
        return -1_000

    latin = len(re.findall(r"[A-Za-z]", text))
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    kana = len(re.findall(r"[\u3040-\u30ff]", text))
    hangul = len(re.findall(r"[\uac00-\ud7af]", text))
    digits = len(re.findall(r"\d", text))
    useful = latin + cjk + kana + hangul + digits
    noise = len(re.findall(r"[^A-Za-z0-9\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af\s.,:;!?()\\/\-+_%#@&=]", text))
    cyrillic = len(re.findall(r"[\u0400-\u052f]", text))

    score = useful * 3 - noise * 4 - cyrillic * 6
    if latin >= 3:
        score += 8
    if useful < 2:
        score -= 12
    return score


def ocr_image_text(image):
    ensure_ocr_ready()

    candidates = {}
    languages = [OCR_LANGUAGE, "eng"]
    page_modes = [7, 6, 11]
    for variant in create_ocr_variants(image):
        for language in languages:
            for page_mode in page_modes:
                config = f"--oem 1 --psm {page_mode} -c preserve_interword_spaces=1"
                try:
                    text = pytesseract.image_to_string(variant, lang=language, config=config)
                except Exception:
                    continue

                cleaned = clean_ocr_candidate(text)
                if cleaned:
                    score, count = candidates.get(cleaned, (-1_000, 0))
                    candidates[cleaned] = (max(score, score_ocr_candidate(cleaned)), count + 1)

    if not candidates:
        raise RuntimeError("没有识别到文字，请重新框选更清晰的区域。")

    def rank_candidate(item):
        text, (score, count) = item
        line_penalty = (text.count("\n")) * 3
        return score + count * 4 - line_penalty, count, -len(text)

    return max(candidates.items(), key=rank_candidate)[0]


def guess_source_language(text):
    if re.search(r"[\u4e00-\u9fff]", text):
        return "zh-CN"
    if re.search(r"[\u3040-\u30ff]", text):
        return "ja"
    if re.search(r"[\uac00-\ud7af]", text):
        return "ko"
    return "en"


def resolve_language_pair(text, choice_label):
    source, target = LANGUAGE_CHOICES[choice_label]
    guessed_source = guess_source_language(text)

    if source == "auto" and target == "auto":
        source = guessed_source
        target = "en" if source == "zh-CN" else "zh-CN"
    elif source == "auto":
        source = guessed_source

    return source, target


def translate_with_google(text, source, target):
    params = parse.urlencode(
        {
            "client": "gtx",
            "sl": source,
            "tl": target,
            "dt": "t",
            "ie": "UTF-8",
            "oe": "UTF-8",
            "q": text,
        }
    )
    req = request.Request(
        f"{GOOGLE_TRANSLATE_URL}?{params}",
        headers={"User-Agent": f"{APP_NAME}/0.1"},
    )

    with request.urlopen(req, timeout=12) as response:
        payload = json.load(response)

    translated_parts = []
    for sentence in payload[0] or []:
        if isinstance(sentence, list) and sentence and sentence[0]:
            translated_parts.append(sentence[0])

    translated = "".join(translated_parts).strip()
    if not translated:
        raise RuntimeError("Google Translate returned an empty response.")
    return html.unescape(translated)


def translate_with_mymemory(text, source, target):
    params = parse.urlencode({"q": text, "langpair": f"{source}|{target}"})
    req = request.Request(
        f"{MYMEMORY_TRANSLATE_URL}?{params}",
        headers={"User-Agent": f"{APP_NAME}/0.1"},
    )

    with request.urlopen(req, timeout=12) as response:
        payload = json.load(response)

    status = payload.get("responseStatus")
    if status not in (200, "200"):
        details = payload.get("responseDetails") or "MyMemory translation failed."
        raise RuntimeError(details)

    translated = payload.get("responseData", {}).get("translatedText", "").strip()
    if not translated:
        raise RuntimeError("MyMemory returned an empty response.")
    return html.unescape(translated)


def translate_text(text, choice_label):
    source, target = resolve_language_pair(text, choice_label)
    if source == target:
        return text

    try:
        return translate_with_google(text, source, target)
    except (RuntimeError, OSError, error.URLError, json.JSONDecodeError):
        return translate_with_mymemory(text, source, target)


class TranslatorApp:
    def __init__(self, root):
        self.root = root
        self.active_request_id = 0
        self.is_translating = False
        self.is_capturing = False
        self.main_hidden_for_screenshot = False
        self.idle_after_id = None
        self.floating_window = None
        self.capture_overlay = None
        self.result_window = None
        self.is_collapsed = False
        self.last_main_geometry = None
        self.last_floating_geometry = None
        self.screenshot_return_state = "main"
        self.capture_bounds = None
        self.capture_canvas = None
        self.capture_start = None
        self.capture_rectangle = None
        self.drag_origin = None
        self.drag_moved = False

        self.pin_var = tk.BooleanVar(value=True)
        self.language_var = tk.StringVar(value=next(iter(LANGUAGE_CHOICES)))
        self.status_var = tk.StringVar(value="输入文字后按 Ctrl+Enter 翻译")

        self.configure_window()
        self.configure_style()
        self.build_ui()
        self.bind_shortcuts()
        self.reset_idle_timer()

    def configure_window(self):
        self.root.title(f"{APP_NAME} 翻译浮窗")
        self.root.minsize(620, 380)
        self.root.configure(bg="#f5f7fb")
        self.root.attributes("-topmost", True)
        self.root.protocol("WM_DELETE_WINDOW", self.close_app)

        width = 760
        height = 460
        screen_width = self.root.winfo_screenwidth()
        x = max(32, screen_width - width - 48)
        y = 72
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def configure_style(self):
        default_font = font.nametofont("TkDefaultFont")
        default_font.configure(family="Microsoft YaHei UI", size=10)

        text_font = font.nametofont("TkTextFont")
        text_font.configure(family="Microsoft YaHei UI", size=11)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Root.TFrame", background="#f5f7fb")
        style.configure("Panel.TFrame", background="#ffffff", borderwidth=1, relief="solid")
        style.configure("Toolbar.TFrame", background="#f5f7fb")
        style.configure("Title.TLabel", background="#f5f7fb", foreground="#172033", font=("Microsoft YaHei UI", 14, "bold"))
        style.configure("Subtle.TLabel", background="#ffffff", foreground="#667085")
        style.configure("Status.TLabel", background="#f5f7fb", foreground="#475467")
        style.configure("TLabel", background="#f5f7fb", foreground="#172033")
        style.configure("TButton", padding=(12, 8), font=("Microsoft YaHei UI", 10))
        style.configure("Primary.TButton", padding=(16, 8), foreground="#ffffff", background="#2563eb")
        style.map(
            "Primary.TButton",
            background=[("disabled", "#9bb7f3"), ("active", "#1d4ed8"), ("pressed", "#1e40af")],
            foreground=[("disabled", "#eef4ff")],
        )
        style.configure("TCheckbutton", background="#f5f7fb", foreground="#172033")
        style.configure("TCombobox", padding=(8, 6))

    def build_ui(self):
        root_frame = ttk.Frame(self.root, style="Root.TFrame", padding=18)
        root_frame.pack(fill="both", expand=True)
        root_frame.columnconfigure(0, weight=1)
        root_frame.rowconfigure(2, weight=1)

        header = ttk.Frame(root_frame, style="Toolbar.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        title_block = ttk.Frame(header, style="Toolbar.TFrame")
        title_block.grid(row=0, column=0, sticky="w")
        ttk.Label(title_block, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            title_block,
            text="轻量桌面翻译器",
            background="#f5f7fb",
            foreground="#667085",
        ).pack(anchor="w", pady=(2, 0))

        ttk.Checkbutton(
            header,
            text="置顶",
            variable=self.pin_var,
            command=self.toggle_topmost,
        ).grid(row=0, column=1, sticky="e", padx=(12, 0))

        controls = ttk.Frame(root_frame, style="Toolbar.TFrame")
        controls.grid(row=1, column=0, sticky="ew", pady=(16, 12))
        controls.columnconfigure(1, weight=1)

        ttk.Label(controls, text="方向").grid(row=0, column=0, sticky="w", padx=(0, 8))
        language = ttk.Combobox(
            controls,
            textvariable=self.language_var,
            values=list(LANGUAGE_CHOICES),
            state="readonly",
            width=18,
        )
        language.grid(row=0, column=1, sticky="w")

        self.screenshot_button = ttk.Button(
            controls,
            text="截图翻译",
            command=self.start_screenshot_translation,
        )
        self.screenshot_button.grid(row=0, column=2, padx=(8, 0))
        ttk.Button(controls, text="收起", command=self.collapse_to_floating_button).grid(row=0, column=3, padx=(8, 0))
        ttk.Button(controls, text="清空", command=self.clear_all).grid(row=0, column=4, padx=(8, 0))
        ttk.Button(controls, text="复制结果", command=self.copy_result).grid(row=0, column=5, padx=(8, 0))
        self.translate_button = ttk.Button(
            controls,
            text="翻译",
            style="Primary.TButton",
            command=self.start_translation,
        )
        self.translate_button.grid(row=0, column=6, padx=(8, 0))

        content = ttk.Frame(root_frame, style="Root.TFrame")
        content.grid(row=2, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1, uniform="text")
        content.columnconfigure(1, weight=1, uniform="text")
        content.rowconfigure(0, weight=1)

        input_panel = ttk.Frame(content, style="Panel.TFrame", padding=12)
        input_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        input_panel.rowconfigure(1, weight=1)
        input_panel.columnconfigure(0, weight=1)
        ttk.Label(input_panel, text="输入", style="Subtle.TLabel").grid(row=0, column=0, sticky="w")

        self.input_text = tk.Text(
            input_panel,
            wrap="word",
            undo=True,
            relief="flat",
            borderwidth=0,
            padx=4,
            pady=8,
            bg="#ffffff",
            fg="#101828",
            insertbackground="#2563eb",
            selectbackground="#dbeafe",
            selectforeground="#101828",
        )
        self.input_text.grid(row=1, column=0, sticky="nsew")
        self.input_text.focus_set()

        output_panel = ttk.Frame(content, style="Panel.TFrame", padding=12)
        output_panel.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        output_panel.rowconfigure(1, weight=1)
        output_panel.columnconfigure(0, weight=1)
        ttk.Label(output_panel, text="译文", style="Subtle.TLabel").grid(row=0, column=0, sticky="w")

        self.output_text = tk.Text(
            output_panel,
            wrap="word",
            relief="flat",
            borderwidth=0,
            padx=4,
            pady=8,
            bg="#ffffff",
            fg="#101828",
            insertbackground="#2563eb",
            selectbackground="#dbeafe",
            selectforeground="#101828",
            state="disabled",
        )
        self.output_text.grid(row=1, column=0, sticky="nsew")

        footer = ttk.Frame(root_frame, style="Toolbar.TFrame")
        footer.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.status_var, style="Status.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            footer,
            text="Ctrl+Enter 翻译 / Ctrl+Shift+S 截图 / Esc 清空",
            style="Status.TLabel",
        ).grid(row=0, column=1, sticky="e")

    def bind_shortcuts(self):
        self.root.bind("<Escape>", self.clear_all)
        self.root.bind("<Control-Return>", self.start_translation)
        self.root.bind("<Control-Shift-S>", self.start_screenshot_translation)
        self.root.bind("<Control-Shift-s>", self.start_screenshot_translation)
        self.root.bind("<FocusIn>", self.handle_activity)
        self.root.bind("<FocusOut>", self.handle_activity)
        self.input_text.bind("<Control-Return>", self.start_translation)
        self.root.bind_all("<KeyPress>", self.handle_activity, add="+")
        self.root.bind_all("<ButtonPress>", self.handle_activity, add="+")
        self.root.bind_all("<MouseWheel>", self.handle_activity, add="+")
        self.root.bind_all("<Motion>", self.handle_activity, add="+")

    def toggle_topmost(self):
        self.root.attributes("-topmost", self.pin_var.get())
        if self.floating_window is not None and self.floating_window.winfo_exists():
            self.floating_window.attributes("-topmost", True)

    def cancel_idle_timer(self):
        if self.idle_after_id is not None:
            self.root.after_cancel(self.idle_after_id)
            self.idle_after_id = None

    def reset_idle_timer(self):
        if self.is_collapsed or self.is_capturing or self.main_hidden_for_screenshot:
            return

        self.cancel_idle_timer()
        if not self.is_translating:
            self.idle_after_id = self.root.after(IDLE_COLLAPSE_MS, self.collapse_to_floating_button)

    def handle_activity(self, event=None):
        self.reset_idle_timer()

    def calculate_floating_geometry(self):
        if self.last_floating_geometry:
            return self.last_floating_geometry

        self.root.update_idletasks()
        match = re.match(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", self.root.geometry())
        if match:
            width, _, x, y = map(int, match.groups())
            button_x = max(12, x + width - FLOATING_BUTTON_WIDTH - 18)
            button_y = max(32, y + 18)
        else:
            screen_width = self.root.winfo_screenwidth()
            button_x = max(12, screen_width - FLOATING_BUTTON_WIDTH - 48)
            button_y = 96

        return f"{FLOATING_BUTTON_WIDTH}x{FLOATING_BUTTON_HEIGHT}+{button_x}+{button_y}"

    def collapse_to_floating_button(self, event=None):
        if self.is_translating or self.is_collapsed:
            return "break"

        self.cancel_idle_timer()
        self.root.update_idletasks()
        self.last_main_geometry = self.root.geometry()
        self.is_collapsed = True

        if self.floating_window is None or not self.floating_window.winfo_exists():
            self.create_floating_button()

        self.floating_window.geometry(self.calculate_floating_geometry())
        self.floating_window.deiconify()
        self.floating_window.lift()
        self.root.withdraw()
        return "break"

    def create_floating_button(self):
        self.floating_window = tk.Toplevel(self.root)
        self.floating_window.title(f"{APP_NAME}，点击展开")
        self.floating_window.overrideredirect(True)
        self.floating_window.resizable(False, False)
        self.floating_window.configure(bg="#1d4ed8")
        self.floating_window.attributes("-topmost", True)

        button = tk.Label(
            self.floating_window,
            text="译",
            bg="#2563eb",
            fg="#ffffff",
            font=("Microsoft YaHei UI", 20, "bold"),
            width=4,
            height=2,
            cursor="hand2",
            bd=0,
        )
        button.pack(fill="both", expand=True)

        for widget in (self.floating_window, button):
            widget.bind("<ButtonPress-1>", self.start_floating_drag)
            widget.bind("<B1-Motion>", self.move_floating_button)
            widget.bind("<ButtonRelease-1>", self.finish_floating_click)
            widget.bind("<Enter>", lambda _event, label=button: label.configure(bg="#1d4ed8"))
            widget.bind("<Leave>", lambda _event, label=button: label.configure(bg="#2563eb"))

    def start_floating_drag(self, event):
        self.drag_origin = (event.x_root, event.y_root)
        self.drag_moved = False

    def move_floating_button(self, event):
        if self.drag_origin is None or self.floating_window is None:
            return

        start_x, start_y = self.drag_origin
        delta_x = event.x_root - start_x
        delta_y = event.y_root - start_y
        if abs(delta_x) > 3 or abs(delta_y) > 3:
            self.drag_moved = True

        current_x = self.floating_window.winfo_x()
        current_y = self.floating_window.winfo_y()
        new_x = max(0, current_x + delta_x)
        new_y = max(0, current_y + delta_y)
        self.floating_window.geometry(f"+{new_x}+{new_y}")
        self.drag_origin = (event.x_root, event.y_root)
        self.last_floating_geometry = (
            f"{FLOATING_BUTTON_WIDTH}x{FLOATING_BUTTON_HEIGHT}+{new_x}+{new_y}"
        )

    def finish_floating_click(self, event):
        if not self.drag_moved:
            self.restore_from_floating_button()

        self.drag_origin = None
        self.drag_moved = False

    def restore_from_floating_button(self):
        if self.floating_window is not None and self.floating_window.winfo_exists():
            self.last_floating_geometry = self.floating_window.geometry()
            self.floating_window.withdraw()

        self.root.deiconify()
        if self.last_main_geometry:
            self.root.geometry(self.last_main_geometry)
        self.root.attributes("-topmost", self.pin_var.get())
        self.root.lift()
        self.input_text.focus_set()
        self.is_collapsed = False
        self.reset_idle_timer()

    def close_app(self):
        self.cancel_idle_timer()
        if self.capture_overlay is not None and self.capture_overlay.winfo_exists():
            self.capture_overlay.destroy()
        if self.result_window is not None and self.result_window.winfo_exists():
            self.result_window.destroy()
        if self.floating_window is not None and self.floating_window.winfo_exists():
            self.floating_window.destroy()
        self.root.destroy()

    def start_screenshot_translation(self, event=None):
        if self.is_translating or self.is_capturing:
            return "break"

        try:
            ensure_ocr_ready()
        except Exception as exc:
            self.status_var.set(f"截图翻译不可用：{exc}")
            self.reset_idle_timer()
            return "break"

        self.cancel_idle_timer()
        self.is_capturing = True
        self.main_hidden_for_screenshot = True
        self.screenshot_return_state = "collapsed" if self.is_collapsed else "main"
        self.root.update_idletasks()
        self.last_main_geometry = self.root.geometry()
        self.status_var.set("正在准备截图框选...")

        if self.result_window is not None and self.result_window.winfo_exists():
            self.result_window.destroy()
            self.result_window = None
        if self.floating_window is not None and self.floating_window.winfo_exists():
            self.floating_window.withdraw()

        self.root.withdraw()
        self.root.after(160, self.show_capture_overlay)
        return "break"

    def show_capture_overlay(self):
        self.capture_bounds = get_virtual_screen_bounds(self.root)
        left, top, width, height = self.capture_bounds

        self.capture_overlay = tk.Toplevel(self.root)
        self.capture_overlay.overrideredirect(True)
        self.capture_overlay.attributes("-topmost", True)
        self.capture_overlay.attributes("-alpha", 0.28)
        self.capture_overlay.configure(bg="#111827")
        self.capture_overlay.geometry(format_geometry(width, height, left, top))

        self.capture_canvas = tk.Canvas(
            self.capture_overlay,
            bg="#111827",
            highlightthickness=0,
            cursor="crosshair",
        )
        self.capture_canvas.pack(fill="both", expand=True)
        self.capture_canvas.bind("<ButtonPress-1>", self.start_capture_selection)
        self.capture_canvas.bind("<B1-Motion>", self.update_capture_selection)
        self.capture_canvas.bind("<ButtonRelease-1>", self.finish_capture_selection)
        self.capture_overlay.bind("<Escape>", self.cancel_screenshot_capture)
        self.capture_overlay.focus_force()

    def start_capture_selection(self, event):
        self.capture_start = (event.x, event.y)
        if self.capture_rectangle is not None:
            self.capture_canvas.delete(self.capture_rectangle)
        self.capture_rectangle = self.capture_canvas.create_rectangle(
            event.x,
            event.y,
            event.x,
            event.y,
            outline="#60a5fa",
            width=3,
        )

    def update_capture_selection(self, event):
        if self.capture_start is None or self.capture_rectangle is None:
            return

        start_x, start_y = self.capture_start
        self.capture_canvas.coords(self.capture_rectangle, start_x, start_y, event.x, event.y)

    def finish_capture_selection(self, event):
        if self.capture_start is None or self.capture_bounds is None:
            self.cancel_screenshot_capture()
            return

        start_x, start_y = self.capture_start
        end_x, end_y = event.x, event.y
        width = abs(end_x - start_x)
        height = abs(end_y - start_y)
        if width < MIN_CAPTURE_SIZE or height < MIN_CAPTURE_SIZE:
            self.cancel_screenshot_capture(status="截图已取消")
            return

        left, top, _, _ = self.capture_bounds
        box = (
            left + min(start_x, end_x),
            top + min(start_y, end_y),
            left + max(start_x, end_x),
            top + max(start_y, end_y),
        )
        choice = self.language_var.get()

        self.destroy_capture_overlay()
        self.is_capturing = False
        self.status_var.set("识别截图中...")
        self.set_busy(True)

        thread = threading.Thread(
            target=self.worker_screenshot_translate,
            args=(box, choice),
            daemon=True,
        )
        thread.start()

    def cancel_screenshot_capture(self, event=None, status="截图已取消"):
        self.destroy_capture_overlay()
        self.is_capturing = False
        self.status_var.set(status)
        self.restore_after_screenshot()
        return "break"

    def destroy_capture_overlay(self):
        if self.capture_overlay is not None and self.capture_overlay.winfo_exists():
            self.capture_overlay.destroy()
        self.capture_overlay = None
        self.capture_canvas = None
        self.capture_start = None
        self.capture_rectangle = None

    def worker_screenshot_translate(self, box, choice):
        try:
            image = capture_screen_area(box)
            original = ocr_image_text(image)
            translated = translate_text(original, choice)
        except Exception as exc:
            message = str(exc)
            self.root.after(0, lambda: self.finish_screenshot_translation(box, "", "", message))
            return

        self.root.after(0, lambda: self.finish_screenshot_translation(box, original, translated, ""))

    def finish_screenshot_translation(self, box, original, translated, error_message):
        self.set_busy(False)
        if error_message:
            self.restore_after_screenshot()
            self.status_var.set(f"截图翻译失败：{error_message}")
            return

        self.status_var.set("截图翻译完成")
        self.show_screenshot_result(box, original, translated)

    def show_screenshot_result(self, box, original, translated):
        if self.result_window is not None and self.result_window.winfo_exists():
            self.result_window.destroy()

        self.result_window = tk.Toplevel(self.root)
        self.result_window.title(f"{APP_NAME} 截图翻译")
        self.result_window.minsize(380, 280)
        self.result_window.attributes("-topmost", True)
        self.result_window.configure(bg="#f5f7fb")
        self.result_window.protocol("WM_DELETE_WINDOW", self.close_screenshot_result)
        self.result_window.geometry(self.calculate_result_geometry(box))

        container = ttk.Frame(self.result_window, style="Root.TFrame", padding=14)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(1, weight=1)
        container.rowconfigure(3, weight=1)

        ttk.Label(container, text="原文", style="Status.TLabel").grid(row=0, column=0, sticky="w")
        original_text = tk.Text(
            container,
            height=5,
            wrap="word",
            relief="flat",
            borderwidth=0,
            padx=8,
            pady=8,
            bg="#ffffff",
            fg="#101828",
            selectbackground="#dbeafe",
            selectforeground="#101828",
        )
        original_text.grid(row=1, column=0, sticky="nsew", pady=(4, 10))
        original_text.insert("1.0", original)
        original_text.configure(state="disabled")

        ttk.Label(container, text="译文", style="Status.TLabel").grid(row=2, column=0, sticky="w")
        translated_text = tk.Text(
            container,
            height=5,
            wrap="word",
            relief="flat",
            borderwidth=0,
            padx=8,
            pady=8,
            bg="#ffffff",
            fg="#101828",
            selectbackground="#dbeafe",
            selectforeground="#101828",
        )
        translated_text.grid(row=3, column=0, sticky="nsew", pady=(4, 12))
        translated_text.insert("1.0", translated)
        translated_text.configure(state="disabled")

        actions = ttk.Frame(container, style="Root.TFrame")
        actions.grid(row=4, column=0, sticky="e")
        ttk.Button(actions, text="复制译文", command=lambda: self.copy_to_clipboard(translated, "译文已复制")).pack(side="left")
        ttk.Button(actions, text="复制原文", command=lambda: self.copy_to_clipboard(original, "原文已复制")).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="关闭", command=self.close_screenshot_result).pack(side="left", padx=(8, 0))

        self.result_window.lift()
        self.result_window.focus_force()

    def calculate_result_geometry(self, box):
        screen_left, screen_top, screen_width, screen_height = get_virtual_screen_bounds(self.root)
        screen_right = screen_left + screen_width
        screen_bottom = screen_top + screen_height

        right_x = box[2] + 12
        left_x = box[0] - RESULT_WINDOW_WIDTH - 12
        if right_x + RESULT_WINDOW_WIDTH <= screen_right:
            x = right_x
        elif left_x >= screen_left:
            x = left_x
        else:
            x = min(max(box[0], screen_left + 12), screen_right - RESULT_WINDOW_WIDTH - 12)

        y = min(max(box[1], screen_top + 12), screen_bottom - RESULT_WINDOW_HEIGHT - 12)
        return format_geometry(RESULT_WINDOW_WIDTH, RESULT_WINDOW_HEIGHT, x, y)

    def copy_to_clipboard(self, text, status):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status_var.set(status)

    def close_screenshot_result(self):
        if self.result_window is not None and self.result_window.winfo_exists():
            self.result_window.destroy()
        self.result_window = None
        self.restore_after_screenshot()

    def restore_after_screenshot(self):
        self.main_hidden_for_screenshot = False

        if self.screenshot_return_state == "collapsed":
            if self.floating_window is None or not self.floating_window.winfo_exists():
                self.create_floating_button()
            if self.last_floating_geometry:
                self.floating_window.geometry(self.last_floating_geometry)
            else:
                self.floating_window.geometry(self.calculate_floating_geometry())
            self.floating_window.deiconify()
            self.floating_window.lift()
            self.root.withdraw()
            self.is_collapsed = True
        else:
            self.is_collapsed = False
            self.root.deiconify()
            if self.last_main_geometry:
                self.root.geometry(self.last_main_geometry)
            self.root.attributes("-topmost", self.pin_var.get())
            self.root.lift()
            self.input_text.focus_set()

        self.screenshot_return_state = "main"
        self.reset_idle_timer()

    def set_output(self, text):
        self.output_text.configure(state="normal")
        self.output_text.delete("1.0", "end")
        self.output_text.insert("1.0", text)
        self.output_text.configure(state="disabled")

    def set_busy(self, busy):
        self.is_translating = busy
        self.translate_button.configure(state="disabled" if busy else "normal")
        self.screenshot_button.configure(state="disabled" if busy else "normal")
        self.root.configure(cursor="watch" if busy else "")
        if busy:
            self.cancel_idle_timer()
        else:
            self.reset_idle_timer()

    def start_translation(self, event=None):
        if self.is_translating:
            return "break"

        text = self.input_text.get("1.0", "end").strip()
        if not text:
            self.status_var.set("先输入要翻译的文字")
            self.input_text.focus_set()
            return "break"

        self.active_request_id += 1
        request_id = self.active_request_id
        choice = self.language_var.get()

        self.set_busy(True)
        self.status_var.set("翻译中...")

        thread = threading.Thread(
            target=self.worker_translate,
            args=(request_id, text, choice),
            daemon=True,
        )
        thread.start()
        return "break"

    def worker_translate(self, request_id, text, choice):
        try:
            translated = translate_text(text, choice)
        except Exception as exc:  # Keep the UI alive even when the network or API fails.
            self.root.after(0, lambda: self.finish_translation(request_id, "", f"翻译失败：{exc}"))
            return

        self.root.after(0, lambda: self.finish_translation(request_id, translated, "翻译完成"))

    def finish_translation(self, request_id, translated, status):
        if request_id != self.active_request_id:
            return

        self.set_busy(False)
        if translated:
            self.set_output(translated)
        self.status_var.set(status)

    def clear_all(self, event=None):
        self.input_text.delete("1.0", "end")
        self.set_output("")
        self.status_var.set("已清空")
        self.input_text.focus_set()
        self.reset_idle_timer()
        return "break"

    def copy_result(self):
        result = self.output_text.get("1.0", "end").strip()
        if not result:
            self.status_var.set("没有可复制的译文")
            self.reset_idle_timer()
            return

        self.root.clipboard_clear()
        self.root.clipboard_append(result)
        self.status_var.set("译文已复制到剪贴板")
        self.reset_idle_timer()


def main():
    root = tk.Tk()
    TranslatorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
