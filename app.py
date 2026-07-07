import html
import json
import re
import threading
import tkinter as tk
from tkinter import font
from tkinter import ttk
from urllib import error, parse, request


APP_NAME = "FanYI"
GOOGLE_TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
MYMEMORY_TRANSLATE_URL = "https://api.mymemory.translated.net/get"
IDLE_COLLAPSE_MS = 30_000
FLOATING_BUTTON_WIDTH = 68
FLOATING_BUTTON_HEIGHT = 56


LANGUAGE_CHOICES = {
    "自动：中文 ↔ 英文": ("auto", "auto"),
    "英文 → 中文": ("en", "zh-CN"),
    "中文 → 英文": ("zh-CN", "en"),
    "任意语言 → 中文": ("auto", "zh-CN"),
}


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
        self.idle_after_id = None
        self.floating_window = None
        self.is_collapsed = False
        self.last_main_geometry = None
        self.last_floating_geometry = None
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

        ttk.Button(controls, text="收起", command=self.collapse_to_floating_button).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(controls, text="清空", command=self.clear_all).grid(row=0, column=3, padx=(8, 0))
        ttk.Button(controls, text="复制结果", command=self.copy_result).grid(row=0, column=4, padx=(8, 0))
        self.translate_button = ttk.Button(
            controls,
            text="翻译",
            style="Primary.TButton",
            command=self.start_translation,
        )
        self.translate_button.grid(row=0, column=5, padx=(8, 0))

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
            text="Ctrl+Enter 翻译 / Esc 清空 / 空闲30秒收起",
            style="Status.TLabel",
        ).grid(row=0, column=1, sticky="e")

    def bind_shortcuts(self):
        self.root.bind("<Escape>", self.clear_all)
        self.root.bind("<Control-Return>", self.start_translation)
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
        if self.is_collapsed:
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
        if self.floating_window is not None and self.floating_window.winfo_exists():
            self.floating_window.destroy()
        self.root.destroy()

    def set_output(self, text):
        self.output_text.configure(state="normal")
        self.output_text.delete("1.0", "end")
        self.output_text.insert("1.0", text)
        self.output_text.configure(state="disabled")

    def set_busy(self, busy):
        self.is_translating = busy
        self.translate_button.configure(state="disabled" if busy else "normal")
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
