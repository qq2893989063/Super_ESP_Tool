#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ESP 烧录器（图形界面）
======================

基于乐鑫科技（Espressif）开源工具 **esptool** 的图形化 ESP 系列芯片固件烧录器。

主要功能
--------
* **扫描串口**：自动列出本机串口（含 VID/PID、序列号、描述），支持热插拔自动刷新；
* **扫描设备**：逐个探测串口上的芯片，显示芯片型号与状态（无响应 / 串口被占用 / 已识别）；
* **设备信息**：芯片型号、描述、版本、特性、MAC、芯片 ID、晶振、Flash 厂商/容量/电压、
  安全启动、Flash 加密、安全下载模式、USB 模式等；
* **固件烧录**：单文件合并镜像 或 多文件带偏移烧录，支持擦除整片、压缩、校验、
  设置 Flash 模式/大小/频率、烧录后自动复位；
* **高级工具**：擦除 Flash、读取 Flash 到文件、复位设备、读取 MAC、读取 Flash ID；
* **实时日志与进度条**：线程化执行，界面不卡顿。

运行
----
    python esp_flasher_gui.py

无硬件时可用演示模式查看界面：

    python esp_flasher_gui.py --demo

依赖：esptool（本目录源码）及其依赖 pyserial、rich_click、esp-pylib 等。
"""

from __future__ import annotations

import datetime
import os
import queue
import re
import sys
import threading
import time
import traceback

# ---------------------------------------------------------------------------
# 确保使用本目录下的 esptool 源码
# ---------------------------------------------------------------------------
APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, simpledialog, ttk

APP_NAME = "ESP 烧录器"
APP_VERSION = "1.0"
DEMO_MODE = "--demo" in sys.argv
DEMO_AUTORUN = True      # 演示模式下自动扫描并连接（自检脚本会关闭它）


def _cli_tab() -> str:
    """解析 --tab=flash / --tab=tools，用于启动时定位到指定标签页。"""
    for arg in sys.argv:
        if arg.startswith("--tab="):
            return arg.split("=", 1)[1].strip().lower()
    return ""

# ---------------------------------------------------------------------------
# 导入 esptool（失败时给出友好提示，而不是直接崩溃）
# ---------------------------------------------------------------------------
ESPTOOL_IMPORT_ERROR: Exception | None = None
ESPTOOL_VERSION = "?"

try:
    import serial
    from serial.tools import list_ports

    import esptool
    from esptool.cmds import (
        attach_flash,
        detect_chip,
        detect_flash_size,
        erase_flash,
        read_flash,
        reset_chip,
        run_stub,
        verify_flash,
        write_flash,
    )
    from esp_pylib.logger import EspLog, EspLogBase, Verbosity
    from esptool.loader import NotSupportedError

    ESPTOOL_VERSION = getattr(esptool, "__version__", "?")
except Exception as _exc:  # pragma: no cover - 取决于运行环境
    ESPTOOL_IMPORT_ERROR = _exc

# 编译后端（源码 → .bin），独立模块，便于单独测试
COMPILE_IMPORT_ERROR: Exception | None = None
try:
    import esp_compile
except Exception as _exc:  # pragma: no cover
    esp_compile = None
    COMPILE_IMPORT_ERROR = _exc

# ---------------------------------------------------------------------------
# 常量与工具函数
# ---------------------------------------------------------------------------
COLOR_BG = "#f2f3f5"
COLOR_PANEL = "#ffffff"
COLOR_ACCENT = "#e7352c"      # 乐鑫红
COLOR_OK = "#1a9c4b"
COLOR_WARN = "#d98200"
COLOR_ERR = "#d0021b"
COLOR_IDLE = "#9aa0a6"
COLOR_BUSY = "#f0ad4e"
COLOR_TEXT = "#202124"
COLOR_MUTED = "#5f6368"

# 常见 SPI Flash 厂商（JEDEC 制造商标识）
FLASH_VENDORS = {
    0x01: "Spansion/Cypress",
    0x02: "Winbond",
    0x03: "Micron",
    0x04: "Macronix",
    0x05: "ISSI",
    0x0B: "XMC",
    0x0C: "XMC",
    0x1C: "EON",
    0x1F: "Adesto/Atmel",
    0x20: "Micron/XMC",
    0x37: "AMIC",
    0x3D: "Winbond",
    0x5E: "Zbit",
    0x62: "SANYO",
    0x68: "Boya",
    0x6B: "Bestow",
    0x85: "Puya",
    0x8C: "ESMT",
    0x9D: "ISSI",
    0xA1: "FM",
    0xC2: "MXIC (Macronix)",
    0xC8: "GigaDevice",
    0xE0: "BergMicro",
    0xE5: "Zbit",
    0xEB: "Zbit",
    0xEF: "Winbond",
    0xAD: "HYNIX",
}

_RICH_TAG_RE = re.compile(
    r"\[/?(?:bold|dim|italic|underline|blink|reverse|strike|red|green|yellow|blue"
    r"|magenta|cyan|white|black|bright_[a-z]+|on_[a-z]+|link=[^\]]*)[^\]]*\]"
)


def strip_markup(text: str) -> str:
    """去掉 rich 的行内标记，并把 rich 转义过的方括号还原。"""
    text = _RICH_TAG_RE.sub("", text)
    return text.replace("\\[", "[")


def human_size(num_bytes: float) -> str:
    """字节数转可读字符串。"""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.0f} {unit}" if unit == "B" else f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.2f} TB"


def mac_to_str(mac) -> str:
    if not mac:
        return "—"
    return ":".join(f"{b:02X}" for b in mac)


def friendly_port_error(exc: Exception) -> str:
    """把串口异常翻译成中文状态描述。"""
    text = str(exc)
    low = text.lower()
    if isinstance(exc, PermissionError) or "access is denied" in low or "拒绝访问" in text:
        return "串口被占用"
    if "could not open port" in low or "cannot open" in low:
        return "无法打开串口"
    if "permission" in low or "busy" in low:
        return "串口被占用"
    if "failed to connect" in low or "no serial data received" in low:
        return "无响应/非 ESP 设备"
    if isinstance(exc, (serial.SerialException, OSError)):
        return "串口错误"
    return "未识别"


def is_espressif_vid(vid) -> bool:
    return vid in (0x303A, 0x10C4, 0x1A86, 0x0403, 0x067B)


# ---------------------------------------------------------------------------
# GUI 日志器：把 esptool 的输出 / 进度重定向到界面
# ---------------------------------------------------------------------------
class GuiLog(EspLogBase):
    """实现 esp_pylib 日志接口，把内容推送到 GUI 队列。"""

    def __init__(self, sink):
        self._sink = sink          # callable(dict)
        self._verbosity = Verbosity.NORMAL
        self._buf = ""
        self._lock = threading.Lock()

    # -- 内部 ---------------------------------------------------------------
    def _render(self, args) -> str:
        return strip_markup(" ".join("" if a is None else str(a) for a in args))

    def _push(self, text: str, level: str = "info") -> None:
        if text == "":
            return
        self._sink({"t": "log", "level": level, "text": text})

    def _append(self, text: str, end: str) -> None:
        with self._lock:
            self._buf += text
            if "\n" in end:
                line, self._buf = self._buf, ""
                self._push(line.rstrip("\r\n"))
            elif len(self._buf) > 300:
                line, self._buf = self._buf, ""
                self._push(line)

    # -- EspLogBase 接口 ----------------------------------------------------
    def print(self, *args, **kwargs) -> None:
        self._append(self._render(args), str(kwargs.get("end", "\n")))

    def err(self, *args, suggestion: str | None = None) -> None:
        msg = self._render(args)
        if suggestion:
            msg += f"\n提示: {suggestion}"
        self._push(msg, "err")

    def warn(self, *args, suggestion: str | None = None) -> None:
        msg = self._render(args)
        if suggestion:
            msg += f"\n提示: {suggestion}"
        self._push(msg, "warn")

    def note(self, *args) -> None:
        self._push(self._render(args), "note")

    def hint(self, *args) -> None:
        self._push(self._render(args), "hint")

    def debug(self, *args) -> None:
        if self._verbosity >= Verbosity.VERBOSE:
            self._push(self._render(args), "debug")

    def set_verbosity(self, mode) -> None:
        if isinstance(mode, str):
            mode = Verbosity[mode.upper()]
        self._verbosity = mode

    def progress_bar(self, cur_iter, total_iters, prefix="", suffix="", bar_length=30) -> None:
        self._sink(
            {
                "t": "progress",
                "cur": int(cur_iter),
                "total": int(total_iters),
                "text": f"{prefix}{cur_iter}/{total_iters}",
            }
        )

    def counter_line(self, prefix, suffix, *, final: bool = False) -> None:
        self._push(f"{prefix}{suffix}", "note")


# ---------------------------------------------------------------------------
# 演示模式数据（无硬件时用于界面预览 / 自检）
# ---------------------------------------------------------------------------
DEMO_PORTS = [
    {
        "device": "COM3",
        "description": "USB-SERIAL CH340 (COM3)",
        "hwid": "USB VID:PID=1A86:7523 SER=6 LOCATION=1-1",
        "vid": 0x1A86,
        "pid": 0x7523,
        "serial_number": "6",
        "manufacturer": "wch.cn",
        "product": "USB-SERIAL CH340",
    },
    {
        "device": "COM7",
        "description": "USB JTAG/serial debug unit (COM7)",
        "hwid": "USB VID:PID=303A:1001 SER=84:CC:A8:12:34:56",
        "vid": 0x303A,
        "pid": 0x1001,
        "serial_number": "84:CC:A8:12:34:56",
        "manufacturer": "Espressif",
        "product": "USB JTAG/serial debug unit",
    },
    {
        "device": "COM11",
        "description": "Silicon Labs CP210x USB to UART Bridge (COM11)",
        "hwid": "USB VID:PID=10C4:EA60 SER=0001",
        "vid": 0x10C4,
        "pid": 0xEA60,
        "serial_number": "0001",
        "manufacturer": "Silicon Labs",
        "product": "CP210x UART Bridge",
    },
]

DEMO_CHIPS = {"COM7": "ESP32-S3", "COM11": "ESP32-C3"}
DEMO_BAD_PORTS = {"COM3": "无响应/非 ESP 设备"}


# ---------------------------------------------------------------------------
# 主界面
# ---------------------------------------------------------------------------
class EspFlasherApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()

        self.title(f"{APP_NAME} v{APP_VERSION}  —  基于 esptool {ESPTOOL_VERSION}")
        self.minsize(900, 600)
        self.configure(bg=COLOR_BG)

        # 高 DPI 缩放系数（1.0 = 100%，2.0 = 200%）
        try:
            self.scale = max(1.0, self.winfo_fpixels("1i") / 96.0)
        except Exception:
            self.scale = 1.0

        # 运行时状态
        self.msg_q: queue.Queue = queue.Queue()
        self.busy = False
        self.cancel_flag = False
        self.esp = None                 # 当前保持连接的 esptool 对象
        self.conn_port: str | None = None
        self.conn_baud: int = 115200
        self.conn_chip: str | None = None      # 已连接设备的芯片名（如 ESP32-C6）
        self.port_rows: dict[str, dict] = {}
        self._no_port_warned = False
        self._worker_local = threading.local()
        self.auto_refresh = tk.BooleanVar(value=True)
        self.device_rows: list[tuple[str, str]] = []

        self._setup_fonts()
        self._setup_style()

        # 重定向 esptool 日志
        self._gui_log = GuiLog(self.msg_q.put)
        if ESPTOOL_IMPORT_ERROR is None:
            try:
                EspLog.set_logger(self._gui_log)
            except Exception:  # pragma: no cover
                pass

        self._build_ui()
        self._log("info", f"{APP_NAME} v{APP_VERSION} 启动")
        if ESPTOOL_IMPORT_ERROR is not None:
            self._log("err", f"esptool 导入失败: {ESPTOOL_IMPORT_ERROR}")
        else:
            self._log("ok", f"esptool {ESPTOOL_VERSION} 加载成功（{os.path.dirname(esptool.__file__)}）")
        if DEMO_MODE:
            self._log("warn", "演示模式：不访问真实串口")

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(60, self._poll_queue)
        self.after(200, self._refresh_ports)
        self.after(1500, self._auto_refresh_tick)
        self.after(900, self._start_env_check)
        if DEMO_MODE and DEMO_AUTORUN:
            self.after(600, self._scan_devices)
            self.after(3000, self._demo_autorun)
        if DEMO_MODE:
            self._demo_prefill()
        tab = _cli_tab()
        if tab:
            self.after(300, lambda: self._select_tab(tab))

    def _select_tab(self, name: str) -> None:
        index = {"info": 0, "device": 0, "flash": 1, "compile": 2,
                 "lib": 3, "library": 3, "tools": 4}.get(name)
        if index is not None:
            try:
                self.notebook.select(index)
            except tk.TclError:
                pass

    def _demo_prefill(self) -> None:
        """演示模式：往烧录表格里放几行示例，便于查看界面。"""
        for path, offset, size in (
            ("bootloader.bin", "0x0", "24 KB"),
            ("partition-table.bin", "0x8000", "3 KB"),
            ("app.bin", "0x10000", "1.21 MB"),
        ):
            self.tree_files.insert("", "end", values=(path, offset, size))
        self.var_single_file.set("factory_merged.bin")
        self.var_read_out.set("flash_dump.bin")

    def _demo_autorun(self) -> None:
        """演示模式：自动连接一个"设备"，方便查看完整界面。"""
        if "COM7" in self.port_rows and not self.busy:
            self.var_port.set("COM7")
            self._connect_device()

    # ------------------------------------------------------------------ 样式
    def px(self, value: float) -> int:
        """把按 96 DPI 设计的像素值换算为当前 DPI 下的像素值。"""
        return int(round(value * getattr(self, "scale", 1.0)))

    def _setup_fonts(self) -> None:
        families = set(tkfont.families(self))
        ui = "TkDefaultFont"
        for name in ("Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑", "Segoe UI"):
            if name in families:
                ui = name
                break
        mono = "Courier New"
        for name in ("Cascadia Mono", "Consolas", "DejaVu Sans Mono"):
            if name in families:
                mono = name
                break
        self.font_ui = (ui, 10)
        self.font_ui_bold = (ui, 10, "bold")
        self.font_title = (ui, 12, "bold")
        self.font_mono = (mono, 9)

    def _setup_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=self.font_ui, background=COLOR_BG, foreground=COLOR_TEXT)
        style.configure("TFrame", background=COLOR_BG)
        style.configure("Panel.TFrame", background=COLOR_PANEL)
        style.configure("TLabel", background=COLOR_BG, foreground=COLOR_TEXT)
        style.configure("Muted.TLabel", foreground=COLOR_MUTED)
        style.configure("Title.TLabel", font=self.font_title, foreground=COLOR_TEXT)
        style.configure("TLabelframe", background=COLOR_BG, bordercolor="#d7dade")
        style.configure("TLabelframe.Label", background=COLOR_BG, font=self.font_ui_bold)
        style.configure("TButton", padding=(self.px(10), self.px(5)))
        style.configure("Accent.TButton", padding=(self.px(12), self.px(6)), foreground="#ffffff",
                        background=COLOR_ACCENT)
        style.map("Accent.TButton",
                  background=[("active", "#c72c24"), ("disabled", "#e8a5a1")])
        style.configure("Treeview", background=COLOR_PANEL, fieldbackground=COLOR_PANEL,
                        rowheight=self.px(20), borderwidth=0)
        style.configure("Treeview.Heading", font=self.font_ui_bold, padding=(self.px(4), self.px(4)))
        style.map("Treeview", background=[("selected", "#cfe4ff")],
                  foreground=[("selected", COLOR_TEXT)])
        style.configure("TNotebook", background=COLOR_BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(self.px(16), self.px(7)), font=self.font_ui)
        style.map("TNotebook.Tab", background=[("selected", COLOR_PANEL)])
        style.configure("TEntry", padding=self.px(3))
        style.configure("TCombobox", padding=self.px(2))
        style.configure("TCheckbutton", background=COLOR_BG)
        style.configure("TProgressbar", thickness=self.px(16))

    # --------------------------------------------------------------- 界面构建
    def _build_ui(self) -> None:
        self._build_toolbar()
        self._build_bottom()

        outer = ttk.Panedwindow(self, orient="horizontal")
        outer.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self.outer_paned = outer

        left = ttk.Frame(outer)
        right = ttk.Frame(outer)
        outer.add(left, weight=1)
        outer.add(right, weight=2)

        self._build_port_panel(left)
        self._build_tabs(right)
        self.after(200, self._fit_window)

    def _fit_window(self) -> None:
        """根据 DPI 与实际内容请求尺寸，确定窗口大小与左右分栏位置。

        高度按**所有标签页里最高的那个**算，否则切到内容高的页面（如“库管理”）
        时，底部控件会被挤扁。
        """
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        notebook_h = self.notebook.winfo_height()
        chrome = (self.winfo_height() - notebook_h) if notebook_h > 1 else self.px(380)
        tabs = [self.tab_info, self.tab_flash, self.tab_compile, self.tab_lib, self.tab_tools]
        need_h = max((t.winfo_reqheight() for t in tabs), default=self.px(600)) + chrome
        w = min(max(self.winfo_reqwidth(), self.px(1120)), sw - self.px(40))
        h = min(max(need_h, self.px(700)), sh - self.px(40))
        w, h = max(w, self.px(900)), max(h, self.px(600))
        self.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 4)}")
        self.minsize(min(self.px(960), w), min(self.px(620), h))
        self.update_idletasks()
        self._set_sash(min(self.px(560), int(w * 0.42)))

    def _set_sash(self, pos: int) -> None:
        try:
            self.outer_paned.sashpos(0, pos)
        except tk.TclError:
            pass

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self, padding=(10, 8, 10, 4))
        bar.pack(fill="x")

        row1 = ttk.Frame(bar)
        row1.pack(fill="x")
        ttk.Label(row1, text="串口:", font=self.font_ui_bold).pack(side="left")
        self.var_port = tk.StringVar()
        self.cmb_port = ttk.Combobox(row1, textvariable=self.var_port, width=12, state="readonly")
        self.cmb_port.pack(side="left", padx=(4, 12))
        self.cmb_port.bind("<<ComboboxSelected>>", lambda _e: self._on_port_selected())

        ttk.Label(row1, text="波特率:", font=self.font_ui_bold).pack(side="left")
        self.var_baud = tk.StringVar(value="921600")
        ttk.Combobox(row1, textvariable=self.var_baud, width=9, state="readonly",
                     values=["115200", "230400", "460800", "921600", "1500000", "2000000"]
                     ).pack(side="left", padx=(4, 12))

        ttk.Label(row1, text="连接方式:", font=self.font_ui_bold).pack(side="left")
        self.var_before = tk.StringVar(value="default-reset")
        ttk.Combobox(row1, textvariable=self.var_before, width=15, state="readonly",
                     values=["default-reset", "usb-reset", "no-reset", "no-reset-no-sync"]
                     ).pack(side="left", padx=(4, 12))

        self.var_stub = tk.BooleanVar(value=True)
        ttk.Checkbutton(row1, text="使用 Stub 引导（更快）",
                        variable=self.var_stub).pack(side="left", padx=(0, 12))

        row2 = ttk.Frame(bar)
        row2.pack(fill="x", pady=(6, 0))
        self.btn_refresh = ttk.Button(row2, text="刷新端口", command=self._refresh_ports)
        self.btn_refresh.pack(side="left", padx=(0, 6))
        self.btn_scan = ttk.Button(row2, text="扫描设备", command=self._scan_devices)
        self.btn_scan.pack(side="left", padx=6)
        self.btn_connect = ttk.Button(row2, text="连接 / 识别", style="Accent.TButton",
                                      command=self._connect_device)
        self.btn_connect.pack(side="left", padx=6)
        self.btn_disconnect = ttk.Button(row2, text="断开", command=self._disconnect_clicked)
        self.btn_disconnect.pack(side="left", padx=6)
        ttk.Label(row2, style="Muted.TLabel",
                  text="双击串口列表中的端口也可以直接连接设备").pack(side="left", padx=12)

    def _build_port_panel(self, parent) -> None:
        box = ttk.Labelframe(parent, text=" 串口列表 ", padding=6)
        box.pack(fill="both", expand=True, padx=(0, 4))

        cols = ("port", "chip", "status", "desc")
        self.tree_ports = ttk.Treeview(box, columns=cols, show="headings", selectmode="browse",
                                       height=12)
        headings = {"port": ("端口", 66, "w"), "chip": ("芯片型号", 96, "w"),
                    "status": ("状态", 145, "w"), "desc": ("描述", 190, "w")}
        for key, (text, width, anchor) in headings.items():
            self.tree_ports.heading(key, text=text)
            self.tree_ports.column(key, width=self.px(width), anchor=anchor,
                                   stretch=(key == "desc"))
        self.tree_ports.tag_configure("ok", foreground=COLOR_OK)
        self.tree_ports.tag_configure("err", foreground=COLOR_MUTED)
        self.tree_ports.tag_configure("busy", foreground=COLOR_WARN)

        vs = ttk.Scrollbar(box, orient="vertical", command=self.tree_ports.yview)
        hs = ttk.Scrollbar(box, orient="horizontal", command=self.tree_ports.xview)
        self.tree_ports.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.tree_ports.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        self.tree_ports.bind("<Double-1>", lambda _e: self._connect_device())
        self.tree_ports.bind("<<TreeviewSelect>>", lambda _e: self._on_port_selected())

        bottom = ttk.Frame(box)
        bottom.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Checkbutton(bottom, text="自动刷新", variable=self.auto_refresh).pack(side="left")
        ttk.Button(bottom, text="端口诊断", command=self._start_port_diag).pack(side="left", padx=8)
        ttk.Label(bottom, text="（双击端口可直接连接）", style="Muted.TLabel").pack(side="left", padx=8)

    def _build_tabs(self, parent) -> None:
        nb = ttk.Notebook(parent)
        nb.pack(fill="both", expand=True, padx=(4, 0))
        self.notebook = nb

        self.tab_info = ttk.Frame(nb, padding=8)
        self.tab_flash = ttk.Frame(nb, padding=8)
        self.tab_compile = ttk.Frame(nb, padding=8)
        self.tab_lib = ttk.Frame(nb, padding=6)
        self.tab_tools = ttk.Frame(nb, padding=8)
        nb.add(self.tab_info, text="设备信息")
        nb.add(self.tab_flash, text="烧录固件")
        nb.add(self.tab_compile, text="编译固件")
        nb.add(self.tab_lib, text="库管理")
        nb.add(self.tab_tools, text="高级工具")
        nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self._build_info_tab()
        self._build_flash_tab()
        self._build_compile_tab()
        self._build_library_tab()
        self._build_tools_tab()

    def _on_tab_changed(self, _event=None) -> None:
        """切到“库管理”页时自动刷新已安装库列表，并保证页面放得下。"""
        self._ensure_tab_fits()
        if self.notebook.index("current") == 3:
            # 窗口尺寸变化后分栏位置会被重置，这里多设两次
            self.after(60, self._set_lib_sash)
            self.after(500, self._set_lib_sash)
        try:
            if self.notebook.index("current") == 3 and not self.busy:
                if not self.tree_libs.get_children():
                    self._start_lib_list()
        except (tk.TclError, AttributeError):
            pass

    def _ensure_tab_fits(self) -> None:
        """当前页内容比窗口高时把窗口长高（不超过屏幕），避免控件被挤扁。"""
        try:
            tab = self.notebook.nametowidget(self.notebook.select())
            chrome = self.winfo_height() - self.notebook.winfo_height()
            need = tab.winfo_reqheight() + chrome
            if need > self.winfo_height() + self.px(8):
                height = min(need, self.winfo_screenheight() - self.px(40))
                if height > self.winfo_height():
                    self.geometry(f"{self.winfo_width()}x{height}")
        except Exception:
            pass

    def _build_info_tab(self) -> None:
        top = ttk.Frame(self.tab_info)
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, text="设备信息", style="Title.TLabel").pack(side="left")
        ttk.Button(top, text="复制信息", command=self._copy_info).pack(side="right", padx=3)
        ttk.Button(top, text="重新读取", command=self._reload_info).pack(side="right", padx=3)

        wrap = ttk.Frame(self.tab_info)
        wrap.pack(fill="both", expand=True)
        self.tree_info = ttk.Treeview(wrap, columns=("item", "value"), show="headings", height=14)
        self.tree_info.heading("item", text="项目")
        self.tree_info.heading("value", text="值")
        self.tree_info.column("item", width=self.px(140), anchor="w", stretch=False)
        self.tree_info.column("value", width=self.px(360), anchor="w")
        self.tree_info.tag_configure("section", background="#e9edf2", font=self.font_ui_bold)
        self.tree_info.tag_configure("ok", foreground=COLOR_OK)
        self.tree_info.tag_configure("warn", foreground=COLOR_WARN)
        self.tree_info.tag_configure("err", foreground=COLOR_ERR)
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tree_info.yview)
        self.tree_info.configure(yscrollcommand=vs.set)
        self.tree_info.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self._show_device_rows([("— 提示 —", "尚未连接设备。请选择串口后点击“连接 / 识别”。")])

    def _build_flash_tab(self) -> None:
        mode = ttk.Frame(self.tab_flash)
        mode.pack(fill="x")
        self.var_mode = tk.StringVar(value="multi")
        ttk.Radiobutton(mode, text="多文件烧录（每个文件指定地址）", value="multi",
                        variable=self.var_mode, command=self._update_flash_mode).pack(side="left")
        ttk.Radiobutton(mode, text="单个合并镜像（factory.bin）", value="single",
                        variable=self.var_mode, command=self._update_flash_mode).pack(side="left", padx=16)

        # --- 单文件模式 ---
        self.frame_single = ttk.Labelframe(self.tab_flash, text=" 镜像文件 ", padding=8)
        self.var_single_file = tk.StringVar()
        self.var_single_addr = tk.StringVar(value="0x0")
        ttk.Entry(self.frame_single, textvariable=self.var_single_file).pack(
            side="left", fill="x", expand=True)
        ttk.Button(self.frame_single, text="浏览…", command=self._pick_single_file).pack(side="left", padx=6)
        ttk.Label(self.frame_single, text="地址:").pack(side="left")
        ttk.Entry(self.frame_single, textvariable=self.var_single_addr, width=10).pack(side="left", padx=4)

        # --- 多文件模式 ---
        self.frame_multi = ttk.Labelframe(self.tab_flash, text=" 固件文件与烧录地址 ", padding=8)
        cols = ("file", "offset", "size")
        self.tree_files = ttk.Treeview(self.frame_multi, columns=cols, show="headings",
                                       height=6, selectmode="extended")
        for key, (text, width, stretch) in {
            "file": ("文件", 420, True), "offset": ("烧录地址", 110, False),
            "size": ("大小", 90, False),
        }.items():
            self.tree_files.heading(key, text=text)
            self.tree_files.column(key, width=self.px(width), anchor="w", stretch=stretch)
        self.tree_files.pack(side="left", fill="both", expand=True)
        self.tree_files.bind("<Double-1>", self._edit_file_offset)
        vs = ttk.Scrollbar(self.frame_multi, orient="vertical", command=self.tree_files.yview)
        self.tree_files.configure(yscrollcommand=vs.set)
        vs.pack(side="left", fill="y")

        btns = ttk.Frame(self.frame_multi)
        btns.pack(side="left", fill="y", padx=(8, 0))
        for text, cmd in (("添加文件", self._add_flash_files), ("删除选中", self._remove_flash_files),
                          ("清空", self._clear_flash_files), ("修改地址", self._edit_file_offset)):
            ttk.Button(btns, text=text, width=10, command=cmd).pack(pady=2)

        # --- 参数 ---
        opts = ttk.Labelframe(self.tab_flash, text=" 烧录参数 ", padding=8)
        self.opts_frame = opts
        opts.pack(fill="x", pady=(6, 4))
        row1 = ttk.Frame(opts)
        row1.pack(fill="x")
        self.var_erase = tk.BooleanVar(value=False)
        self.var_verify = tk.BooleanVar(value=True)
        self.var_compress = tk.BooleanVar(value=True)
        self.var_reset = tk.BooleanVar(value=True)
        self.var_no_stub_flash = tk.BooleanVar(value=False)
        ttk.Checkbutton(row1, text="烧录前擦除整片 Flash", variable=self.var_erase).pack(side="left")
        ttk.Checkbutton(row1, text="烧录后校验", variable=self.var_verify).pack(side="left", padx=10)
        ttk.Checkbutton(row1, text="压缩传输", variable=self.var_compress).pack(side="left", padx=10)
        ttk.Checkbutton(row1, text="完成后复位", variable=self.var_reset).pack(side="left", padx=10)

        row2 = ttk.Frame(opts)
        row2.pack(fill="x", pady=(8, 0))
        ttk.Label(row2, text="Flash 模式:").pack(side="left")
        self.var_flash_mode = tk.StringVar(value="keep")
        ttk.Combobox(row2, textvariable=self.var_flash_mode, width=8, state="readonly",
                     values=["keep", "qio", "qout", "dio", "dout"]).pack(side="left", padx=4)
        ttk.Label(row2, text="Flash 大小:").pack(side="left", padx=(12, 0))
        self.var_flash_size = tk.StringVar(value="keep")
        self.cmb_flash_size = ttk.Combobox(
            row2, textvariable=self.var_flash_size, width=9, state="readonly",
            values=["keep", "detect", "256KB", "512KB", "1MB", "2MB", "4MB",
                    "8MB", "16MB", "32MB", "64MB", "128MB"])
        self.cmb_flash_size.pack(side="left", padx=4)
        ttk.Label(row2, text="Flash 频率:").pack(side="left", padx=(12, 0))
        self.var_flash_freq = tk.StringVar(value="keep")
        self.cmb_flash_freq = ttk.Combobox(
            row2, textvariable=self.var_flash_freq, width=9, state="readonly",
            values=["keep", "20m", "26m", "40m", "80m", "120m"])
        self.cmb_flash_freq.pack(side="left", padx=4)

        action = ttk.Frame(self.tab_flash)
        action.pack(fill="x", pady=8)
        self.btn_flash = ttk.Button(action, text="开始烧录", style="Accent.TButton",
                                    command=self._start_flash)
        self.btn_flash.pack(side="left")
        self.btn_cancel = ttk.Button(action, text="中止", command=self._cancel_task)
        self.btn_cancel.pack(side="left", padx=8)
        ttk.Label(action, style="Muted.TLabel",
                  text="提示：双击表格中的“烧录地址”可修改地址；常见地址 bootloader=0x0、"
                       "partition-table=0x8000、app=0x10000").pack(side="left", padx=10)

        self._update_flash_mode()

    # ------------------------------------------------------- 编译固件（源码→bin）
    def _build_compile_tab(self) -> None:
        top = ttk.Frame(self.tab_compile)
        top.pack(fill="x")
        ttk.Label(top, text="编译方式:", font=self.font_ui_bold).pack(side="left")
        self.var_compile_mode = tk.StringVar(value="arduino")
        for text, value in (("Arduino 工程（.ino/.cpp/.h）", "arduino"),
                            ("ESP-IDF 工程", "idf"),
                            ("ELF → BIN", "elf"),
                            ("合并 BIN → factory.bin", "merge")):
            ttk.Radiobutton(top, text=text, value=value, variable=self.var_compile_mode,
                            command=self._update_compile_mode).pack(side="left", padx=(10, 0))
        ttk.Button(top, text="检测环境", command=self._start_env_check).pack(side="right")

        match_row = ttk.Frame(self.tab_compile)
        match_row.pack(fill="x", pady=(6, 0))
        self.var_auto_match = tk.BooleanVar(value=True)
        ttk.Checkbutton(match_row, text="自动匹配已连接设备（芯片 → 编译环境 / 开发板）",
                        variable=self.var_auto_match,
                        command=self._on_auto_match_toggled).pack(side="left")
        ttk.Button(match_row, text="匹配已连接设备", command=self._match_connected).pack(side="left", padx=8)
        self.lbl_match = ttk.Label(self.tab_compile,
                                   text="尚未连接设备：连接后会自动匹配芯片、编译环境与开发板",
                                   style="Muted.TLabel", wraplength=self.px(1000), justify="left")
        self.lbl_match.pack(fill="x", pady=(4, 0), anchor="w")

        envbox = ttk.Frame(self.tab_compile)
        envbox.pack(fill="x", pady=(6, 0))
        self.lbl_compile_env = ttk.Label(envbox, text="正在检测编译环境…", style="Muted.TLabel",
                                         wraplength=self.px(900), justify="left")
        self.lbl_compile_env.pack(side="left", anchor="w")

        self.compile_body = ttk.Frame(self.tab_compile)
        self.compile_body.pack(fill="both", expand=True, pady=(8, 0))

        self._build_compile_arduino()
        self._build_compile_idf()
        self._build_compile_elf()
        self._build_compile_merge()

        action = ttk.Frame(self.tab_compile)
        action.pack(fill="x", pady=(8, 0))
        self.btn_compile = ttk.Button(action, text="开始编译", style="Accent.TButton",
                                      command=self._start_compile)
        self.btn_compile.pack(side="left")
        ttk.Button(action, text="打开输出目录", command=self._open_output_dir).pack(side="left", padx=6)
        self.btn_compile_cancel = ttk.Button(action, text="中止", command=self._cancel_task)
        self.btn_compile_cancel.pack(side="left", padx=6)
        ttk.Label(action, style="Muted.TLabel",
                  text="编译成功后，产物会自动填入“烧录固件”页，可直接烧录").pack(side="left", padx=10)

        self._update_compile_mode()

    def _build_compile_arduino(self) -> None:
        f = ttk.Labelframe(self.compile_body, text=" Arduino 工程 ", padding=8)
        self.frame_compile_arduino = f

        row1 = ttk.Frame(f)
        row1.pack(fill="x")
        ttk.Label(row1, text="源码目录:").pack(side="left")
        self.var_c_src = tk.StringVar()
        ttk.Entry(row1, textvariable=self.var_c_src).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(row1, text="浏览…", command=self._pick_compile_src).pack(side="left")

        row2 = ttk.Frame(f)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Label(row2, text="开发板 (FQBN):").pack(side="left")
        self.var_c_fqbn = tk.StringVar(value="esp32:esp32:esp32")
        self.cmb_fqbn = ttk.Combobox(row2, textvariable=self.var_c_fqbn, width=26,
                                     values=list(esp_compile.DEFAULT_FQBNS) if esp_compile else [])
        self.cmb_fqbn.pack(side="left", padx=4)
        ttk.Button(row2, text="刷新开发板列表", command=self._start_board_list).pack(side="left", padx=4)
        self.var_c_verbose = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2, text="详细输出", variable=self.var_c_verbose).pack(side="left", padx=8)

        row3 = ttk.Frame(f)
        row3.pack(fill="x", pady=(6, 0))
        ttk.Label(row3, text="输出目录:").pack(side="left")
        self.var_c_out = tk.StringVar()
        ttk.Entry(row3, textvariable=self.var_c_out).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(row3, text="浏览…", command=self._pick_compile_out).pack(side="left")

        row4 = ttk.Frame(f)
        row4.pack(fill="x", pady=(6, 0))
        self.var_c_merge = tk.BooleanVar(value=True)
        ttk.Checkbutton(row4, text="编译后合并为单个 factory.bin（0x0 一个文件烧录）",
                        variable=self.var_c_merge).pack(side="left")
        ttk.Label(f, style="Muted.TLabel", wraplength=self.px(880), justify="left",
                  text="说明：目录内若有 .ino 直接按 sketch 编译；若只有 .cpp/.h，会自动复制成一个临时 "
                       "sketch（代码里需提供 Arduino 风格的 setup()/loop()，并使用 Arduino.h 的 API）。"
                       "输出目录留空时默认使用「源码目录/build_out」。").pack(anchor="w", pady=(6, 0))

    def _build_compile_idf(self) -> None:
        f = ttk.Labelframe(self.compile_body, text=" ESP-IDF 工程 ", padding=8)
        self.frame_compile_idf = f
        row1 = ttk.Frame(f)
        row1.pack(fill="x")
        ttk.Label(row1, text="工程目录:").pack(side="left")
        self.var_i_src = tk.StringVar()
        ttk.Entry(row1, textvariable=self.var_i_src).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(row1, text="浏览…", command=lambda: self._pick_dir(self.var_i_src)).pack(side="left")

        row2 = ttk.Frame(f)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Label(row2, text="目标芯片:").pack(side="left")
        self.var_i_target = tk.StringVar(value="esp32")
        ttk.Combobox(row2, textvariable=self.var_i_target, width=12, state="readonly",
                     values=list(esp_compile.CHIP_CHOICES) if esp_compile else []
                     ).pack(side="left", padx=4)
        self.var_i_merge = tk.BooleanVar(value=True)
        ttk.Checkbutton(row2, text="合并为 factory.bin", variable=self.var_i_merge).pack(side="left", padx=10)
        ttk.Label(f, style="Muted.TLabel", wraplength=self.px(880), justify="left",
                  text="说明：需要已安装 ESP-IDF（含 export.bat）。程序会先加载 IDF 环境再执行 "
                       "idf.py build，然后收集 build/bootloader、partition_table 与应用程序镜像。"
                  ).pack(anchor="w", pady=(6, 0))

    def _build_compile_elf(self) -> None:
        f = ttk.Labelframe(self.compile_body, text=" ELF → BIN ", padding=8)
        self.frame_compile_elf = f
        row1 = ttk.Frame(f)
        row1.pack(fill="x")
        ttk.Label(row1, text="ELF 文件:").pack(side="left")
        self.var_e_elf = tk.StringVar()
        ttk.Entry(row1, textvariable=self.var_e_elf).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(row1, text="浏览…", command=self._pick_elf).pack(side="left")

        row2 = ttk.Frame(f)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Label(row2, text="芯片:").pack(side="left")
        self.var_e_chip = tk.StringVar(value="esp32")
        ttk.Combobox(row2, textvariable=self.var_e_chip, width=12, state="readonly",
                     values=list(esp_compile.CHIP_CHOICES) if esp_compile else []
                     ).pack(side="left", padx=4)
        ttk.Label(row2, text="Flash 模式:").pack(side="left", padx=(10, 0))
        self.var_e_mode = tk.StringVar(value="dio")
        ttk.Combobox(row2, textvariable=self.var_e_mode, width=7, state="readonly",
                     values=["qio", "qout", "dio", "dout"]).pack(side="left", padx=4)
        ttk.Label(row2, text="大小:").pack(side="left", padx=(10, 0))
        self.var_e_size = tk.StringVar(value="4MB")
        ttk.Combobox(row2, textvariable=self.var_e_size, width=7, state="readonly",
                     values=["1MB", "2MB", "4MB", "8MB", "16MB", "32MB"]
                     ).pack(side="left", padx=4)
        ttk.Label(row2, text="频率:").pack(side="left", padx=(10, 0))
        self.var_e_freq = tk.StringVar(value="40m")
        ttk.Combobox(row2, textvariable=self.var_e_freq, width=6, state="readonly",
                     values=["20m", "26m", "40m", "80m"]).pack(side="left", padx=4)

        row3 = ttk.Frame(f)
        row3.pack(fill="x", pady=(6, 0))
        ttk.Label(row3, text="输出 BIN:").pack(side="left")
        self.var_e_out = tk.StringVar()
        ttk.Entry(row3, textvariable=self.var_e_out).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(row3, text="浏览…", command=self._pick_elf_out).pack(side="left")
        ttk.Label(f, style="Muted.TLabel", wraplength=self.px(880), justify="left",
                  text="说明：任何 C/C++ 工具链（ESP-IDF、PlatformIO、Arduino）产出的 .elf 都能转换成 "
                       "可烧录的 .bin，无需再安装其它工具。").pack(anchor="w", pady=(6, 0))

    def _build_compile_merge(self) -> None:
        f = ttk.Labelframe(self.compile_body, text=" 合并 BIN ", padding=8)
        self.frame_compile_merge = f
        row1 = ttk.Frame(f)
        row1.pack(fill="both", expand=True)
        cols = ("addr", "file")
        self.tree_merge = ttk.Treeview(row1, columns=cols, show="headings", height=5)
        self.tree_merge.heading("addr", text="烧录地址")
        self.tree_merge.heading("file", text="镜像文件")
        self.tree_merge.column("addr", width=self.px(110), anchor="w", stretch=False)
        self.tree_merge.column("file", width=self.px(430), anchor="w")
        self.tree_merge.pack(side="left", fill="both", expand=True)
        self.tree_merge.bind("<Double-1>", self._edit_merge_item)
        btns = ttk.Frame(row1)
        btns.pack(side="left", fill="y", padx=(8, 0))
        for text, cmd in (("添加文件", self._add_merge_files), ("删除选中", self._remove_merge_items),
                          ("清空", self._clear_merge_items), ("修改地址", self._edit_merge_item)):
            ttk.Button(btns, text=text, width=10, command=cmd).pack(pady=2)

        row2 = ttk.Frame(f)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Label(row2, text="输出文件:").pack(side="left")
        self.var_m_out = tk.StringVar()
        ttk.Entry(row2, textvariable=self.var_m_out).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(row2, text="浏览…", command=self._pick_merge_out).pack(side="left")
        ttk.Label(row2, text="芯片:").pack(side="left", padx=(10, 0))
        self.var_m_chip = tk.StringVar(value="esp32")
        ttk.Combobox(row2, textvariable=self.var_m_chip, width=11, state="readonly",
                     values=list(esp_compile.CHIP_CHOICES) if esp_compile else []
                     ).pack(side="left", padx=4)

    def _update_compile_mode(self) -> None:
        for frame in (self.frame_compile_arduino, self.frame_compile_idf,
                      self.frame_compile_elf, self.frame_compile_merge):
            frame.pack_forget()
        mode = self.var_compile_mode.get()
        frame = {"arduino": self.frame_compile_arduino, "idf": self.frame_compile_idf,
                 "elf": self.frame_compile_elf, "merge": self.frame_compile_merge}.get(
            mode, self.frame_compile_arduino)
        frame.pack(fill="both", expand=True)
        self.btn_compile.configure(text={
            "arduino": "开始编译", "idf": "开始编译",
            "elf": "转换为 BIN", "merge": "合并 BIN"}.get(mode, "开始编译"))

    # ------------------------------------------------------- 编译：文件选择器
    def _pick_dir(self, var) -> None:
        path = filedialog.askdirectory(title="选择目录")
        if path:
            var.set(os.path.normpath(path))

    def _pick_compile_src(self) -> None:
        path = filedialog.askdirectory(title="选择包含 .ino / .cpp / .h 的目录")
        if not path:
            return
        path = os.path.normpath(path)
        self.var_c_src.set(path)
        if not self.var_c_out.get().strip():
            self.var_c_out.set(os.path.join(path, "build_out"))

    def _pick_compile_out(self) -> None:
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.var_c_out.set(os.path.normpath(path))

    def _pick_elf(self) -> None:
        path = filedialog.askopenfilename(title="选择 ELF 文件",
                                          filetypes=[("ELF 文件", "*.elf"), ("所有文件", "*.*")])
        if path:
            self.var_e_elf.set(os.path.normpath(path))
            if not self.var_e_out.get().strip():
                self.var_e_out.set(os.path.splitext(path)[0] + ".bin")

    def _pick_elf_out(self) -> None:
        path = filedialog.asksaveasfilename(title="保存 BIN 文件", defaultextension=".bin",
                                            filetypes=[("固件镜像", "*.bin"), ("所有文件", "*.*")])
        if path:
            self.var_e_out.set(os.path.normpath(path))

    def _pick_merge_out(self) -> None:
        path = filedialog.asksaveasfilename(title="保存合并后的 BIN", defaultextension=".bin",
                                            filetypes=[("固件镜像", "*.bin"), ("所有文件", "*.*")])
        if path:
            self.var_m_out.set(os.path.normpath(path))

    def _add_merge_files(self) -> None:
        paths = filedialog.askopenfilenames(title="选择要合并的 bin 文件",
                                            filetypes=[("固件镜像", "*.bin"), ("所有文件", "*.*")])
        for path in paths:
            name = os.path.basename(path).lower()
            if "bootloader" in name:
                addr = "0x1000"
            elif "partition" in name:
                addr = "0x8000"
            elif "boot_app0" in name or "ota_data" in name:
                addr = "0xe000"
            else:
                addr = "0x10000"
            self.tree_merge.insert("", "end", values=(addr, os.path.normpath(path)))

    def _remove_merge_items(self) -> None:
        for iid in self.tree_merge.selection():
            self.tree_merge.delete(iid)

    def _clear_merge_items(self) -> None:
        for iid in self.tree_merge.get_children():
            self.tree_merge.delete(iid)

    def _edit_merge_item(self, _event=None) -> None:
        sel = self.tree_merge.selection()
        if not sel:
            return
        iid = sel[0]
        cur = self.tree_merge.item(iid, "values")[0]
        new = simpledialog.askstring("修改烧录地址", "请输入烧录地址（十六进制，如 0x8000）：",
                                     initialvalue=cur, parent=self)
        if new:
            try:
                int(new, 0)
            except ValueError:
                messagebox.showerror(APP_NAME, "地址格式不正确。")
                return
            values = list(self.tree_merge.item(iid, "values"))
            values[0] = new
            self.tree_merge.item(iid, values=values)

    def _open_output_dir(self) -> None:
        path = self._current_output_dir()
        if path and os.path.isdir(path):
            try:
                if hasattr(os, "startfile"):
                    os.startfile(path)  # noqa: S606
                else:
                    subprocess.Popen(["xdg-open", path])
            except Exception as exc:
                messagebox.showerror(APP_NAME, f"无法打开目录：{exc}")
        else:
            messagebox.showinfo(APP_NAME, "输出目录还没有生成，请先编译一次。")

    def _current_output_dir(self) -> str:
        mode = self.var_compile_mode.get()
        if mode == "arduino":
            return self.var_c_out.get().strip() or (
                os.path.join(self.var_c_src.get().strip(), "build_out")
                if self.var_c_src.get().strip() else "")
        if mode == "idf":
            src = self.var_i_src.get().strip()
            return os.path.join(src, "build") if src else ""
        if mode == "elf":
            return os.path.dirname(self.var_e_out.get().strip())
        return os.path.dirname(self.var_m_out.get().strip())

    def _build_tools_tab(self) -> None:
        # 擦除
        erase_box = ttk.Labelframe(self.tab_tools, text=" 擦除 Flash ", padding=10)
        erase_box.pack(fill="x", pady=(0, 8))
        ttk.Label(erase_box, text="擦除整片 Flash 会清空设备上的全部数据（含 NVS、配网信息）。").pack(anchor="w")
        ttk.Button(erase_box, text="擦除整片 Flash", command=self._start_erase).pack(anchor="w", pady=6)

        # 读取
        read_box = ttk.Labelframe(self.tab_tools, text=" 读取 Flash ", padding=10)
        read_box.pack(fill="x", pady=(0, 8))
        line = ttk.Frame(read_box)
        line.pack(fill="x")
        ttk.Label(line, text="起始地址:").pack(side="left")
        self.var_read_addr = tk.StringVar(value="0x0")
        ttk.Entry(line, textvariable=self.var_read_addr, width=12).pack(side="left", padx=4)
        ttk.Label(line, text="长度:").pack(side="left", padx=(10, 0))
        self.var_read_size = tk.StringVar(value="0x100000")
        ttk.Entry(line, textvariable=self.var_read_size, width=12).pack(side="left", padx=4)
        ttk.Label(line, text="保存到:").pack(side="left", padx=(10, 0))
        self.var_read_out = tk.StringVar()
        ttk.Entry(line, textvariable=self.var_read_out).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(line, text="浏览…", command=self._pick_read_out).pack(side="left")
        ttk.Button(read_box, text="开始读取", command=self._start_read).pack(anchor="w", pady=6)

        # 其它
        misc = ttk.Labelframe(self.tab_tools, text=" 设备操作 ", padding=10)
        misc.pack(fill="x")
        ttk.Label(misc, text="复位方式:").pack(side="left")
        self.var_reset_mode = tk.StringVar(value="hard-reset")
        ttk.Combobox(misc, textvariable=self.var_reset_mode, width=16, state="readonly",
                     values=["hard-reset", "soft-reset", "watchdog-reset", "no-reset"]
                     ).pack(side="left", padx=6)
        ttk.Button(misc, text="复位设备", command=self._start_reset).pack(side="left", padx=6)
        ttk.Button(misc, text="读取 Flash ID", command=self._start_flash_id).pack(side="left", padx=6)
        ttk.Button(misc, text="读取 MAC", command=self._start_read_mac).pack(side="left", padx=6)

    def _build_bottom(self) -> None:
        bottom = ttk.Frame(self, padding=(10, 4, 10, 8))
        bottom.pack(side="bottom", fill="x")

        status = ttk.Frame(bottom)
        status.pack(fill="x")
        self.dot = tk.Canvas(status, width=14, height=14, highlightthickness=0, bg=COLOR_BG)
        self.dot.pack(side="left")
        self.dot_id = self.dot.create_oval(2, 2, 12, 12, fill=COLOR_IDLE, outline="")
        self.lbl_status = ttk.Label(status, text="未连接", font=self.font_ui_bold)
        self.lbl_status.pack(side="left", padx=6)
        self.lbl_detail = ttk.Label(status, text="就绪", style="Muted.TLabel")
        self.lbl_detail.pack(side="left", padx=6)

        self.progress = ttk.Progressbar(bottom, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(6, 4))

        logbox = ttk.Labelframe(bottom, text=" 运行日志 ", padding=4)
        logbox.pack(fill="both", expand=True)

        head = ttk.Frame(logbox)
        head.pack(fill="x", pady=(0, 2))
        ttk.Button(head, text="清空", width=6, command=self._clear_log).pack(side="right", padx=2)
        ttk.Button(head, text="保存日志", width=9, command=self._save_log).pack(side="right", padx=2)
        ttk.Label(head, style="Muted.TLabel",
                  text="日志同时反映 esptool 的内部输出，便于排查问题").pack(side="left")

        body = ttk.Frame(logbox)
        body.pack(fill="both", expand=True)
        self.txt_log = tk.Text(body, height=7, wrap="none", font=self.font_mono,
                               bg="#1e1f22", fg="#d7dae0", insertbackground="#d7dae0",
                               relief="flat", borderwidth=0)
        vs = ttk.Scrollbar(body, orient="vertical", command=self.txt_log.yview)
        hs = ttk.Scrollbar(body, orient="horizontal", command=self.txt_log.xview)
        self.txt_log.configure(yscrollcommand=vs.set, xscrollcommand=hs.set, state="disabled")
        self.txt_log.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        for tag, color in (("info", "#d7dae0"), ("ok", "#7ee787"), ("warn", "#f0c674"),
                           ("err", "#ff7b72"), ("note", "#79c0ff"), ("hint", "#a5d6ff"),
                           ("debug", "#8b949e"), ("time", "#6e7681")):
            self.txt_log.tag_configure(tag, foreground=color)

    # ------------------------------------------------------------- 队列与日志
    def _log(self, level: str, text: str) -> None:
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.txt_log.configure(state="normal")
        # 日志太长时裁掉最老的部分，避免长时间编译后界面变慢
        try:
            total = int(self.txt_log.index("end-1c").split(".")[0])
            if total > 4000:
                self.txt_log.delete("1.0", "1000.0")
        except (tk.TclError, ValueError):
            pass
        self.txt_log.insert("end", f"[{stamp}] ", "time")
        self.txt_log.insert("end", text + "\n", level)
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def _poll_queue(self) -> None:
        try:
            while True:
                ev = self.msg_q.get_nowait()
                self._handle_event(ev)
        except queue.Empty:
            pass
        finally:
            self.after(60, self._poll_queue)

    def _handle_event(self, ev: dict) -> None:
        kind = ev.get("t")
        if kind == "log":
            self._log(ev.get("level", "info"), ev.get("text", ""))
        elif kind == "progress":
            total = ev.get("total", 0)
            cur = ev.get("cur", 0)
            if total > 0:
                self.progress.configure(maximum=100, value=cur * 100.0 / total)
                self.lbl_detail.configure(
                    text=f"进度 {cur * 100.0 / total:5.1f}%   ({human_size(cur)} / {human_size(total)})"
                    if total > 4096 else f"进度 {cur}/{total}")
        elif kind == "status":
            self._set_status(ev.get("text", ""), ev.get("kind", "info"), ev.get("detail", ""))
        elif kind == "ports":
            self._apply_ports(ev.get("ports", []))
        elif kind == "probe":
            self._apply_probe(ev)
        elif kind == "device":
            self._show_device_rows(ev.get("rows", []))
            self._apply_flash_caps(ev.get("caps"))
            meta = ev.get("meta") or {}
            if meta.get("chip"):
                self.conn_chip = meta["chip"]
                if self.var_auto_match.get():
                    self._auto_match_device(meta["chip"], meta.get("flash_size"))
        elif kind == "busy":
            self.busy = bool(ev.get("value"))
            self._update_buttons()
        elif kind == "compile_env":
            self.lbl_compile_env.configure(text="　|　".join(ev.get("lines", [])))
        elif kind == "board_list":
            values = ev.get("values") or []
            if values:
                self.cmb_fqbn.configure(values=values)
        elif kind == "compiled":
            self._apply_compiled(ev)
        elif kind == "lib_list":
            self._apply_lib_list(ev)
        elif kind == "lib_search":
            self._apply_lib_search(ev)
        elif kind == "lib_hint":
            self._apply_lib_hint(ev)
        elif kind == "progress_reset":
            self.progress.configure(value=0)

    # ------------------------------------------------------------------ 状态
    def _set_status(self, text: str, kind: str = "info", detail: str = "") -> None:
        colors = {"idle": COLOR_IDLE, "busy": COLOR_BUSY, "ok": COLOR_OK, "err": COLOR_ERR}
        self.dot.itemconfigure(self.dot_id, fill=colors.get(kind, COLOR_IDLE))
        self.lbl_status.configure(text=text)
        if detail:
            self.lbl_detail.configure(text=detail)

    def _update_buttons(self) -> None:
        state = "disabled" if self.busy else "normal"
        for btn in (self.btn_refresh, self.btn_scan, self.btn_connect,
                    self.btn_disconnect, self.btn_flash, self.btn_compile):
            try:
                btn.configure(state=state)
            except tk.TclError:
                pass

    # ------------------------------------------------------------- 串口扫描
    def _refresh_ports(self) -> None:
        if self.msg_q is None:
            return
        if DEMO_MODE:
            self.msg_q.put({"t": "ports", "ports": DEMO_PORTS})
            return
        try:
            ports = []
            for p in list_ports.comports():
                ports.append({
                    "device": p.device,
                    "description": p.description or "",
                    "hwid": p.hwid or "",
                    "vid": p.vid,
                    "pid": p.pid,
                    "serial_number": p.serial_number,
                    "manufacturer": p.manufacturer,
                    "product": p.product,
                })
            self.msg_q.put({"t": "ports", "ports": ports})
        except Exception as exc:  # pragma: no cover
            self.msg_q.put({"t": "log", "level": "err", "text": f"枚举串口失败: {exc}"})

    def _apply_ports(self, ports: list[dict]) -> None:
        seen = set()
        first_new = None
        for info in ports:
            dev = info["device"]
            seen.add(dev)
            vid_pid = (f"{info['vid']:04X}:{info['pid']:04X}"
                       if info.get("vid") and info.get("pid") else "—")
            desc = info.get("description") or vid_pid
            if dev in self.port_rows:
                self.tree_ports.item(self.port_rows[dev]["iid"], values=(
                    dev,
                    self.port_rows[dev]["chip"],
                    self.port_rows[dev]["status"],
                    desc,
                ))
                self.port_rows[dev].update(info=info, desc=desc)
            else:
                iid = self.tree_ports.insert("", "end", values=(dev, "—", "待扫描", desc))
                tag = "ok" if is_espressif_vid(info.get("vid")) else ""
                if tag:
                    self.tree_ports.item(iid, tags=(tag,))
                self.port_rows[dev] = {"iid": iid, "chip": "—", "status": "待扫描",
                                       "info": info, "desc": desc}
                first_new = first_new or dev
        for dev in list(self.port_rows):
            if dev not in seen:
                self.tree_ports.delete(self.port_rows[dev]["iid"])
                del self.port_rows[dev]

        values = list(self.port_rows.keys())
        self.cmb_port.configure(values=values)
        if self.var_port.get() not in values:
            self.var_port.set(values[0] if values else "")
        if first_new:
            self._log("ok", f"发现新串口 {first_new}")
        if values:
            if not self.busy and self.esp is None:
                self.lbl_detail.configure(text=f"发现 {len(values)} 个串口")
            if self._no_port_warned:
                self._no_port_warned = False
                self._log("info", f"串口已出现：{', '.join(values)}")
        else:
            if not self.busy and self.esp is None:
                self.lbl_detail.configure(text="未发现串口，可点击“端口诊断”排查")
            if not self._no_port_warned:
                self._no_port_warned = True
                self._log("warn", "未检测到任何串口，请检查："
                                  "① USB 线是否为数据线（非充电线）；"
                                  "② 串口驱动是否安装（CH340 / CP210x / FTDI / 原生 USB-JTAG）；"
                                  "③ 设备管理器“端口(COM 和 LPT)”下是否有 COM 口。"
                                  "可点击“端口诊断”查看系统层面的详细信息。")

    # ------------------------------------------------------------- 串口诊断
    def _start_port_diag(self) -> None:
        self._start_worker("端口诊断", self._task_port_diag)

    def _task_port_diag(self) -> None:
        self._emit_log("note", "=== 端口诊断报告 ===")
        for line in self._port_diag_lines():
            self._emit_log("info", line)
        self._emit("status", text="端口诊断完成", kind="idle", detail="详见运行日志")

    def _port_diag_lines(self) -> list[str]:
        lines = [
            f"Python {sys.version.split()[0]}   平台 {sys.platform}   "
            f"pyserial {getattr(serial, '__version__', '?')}",
            f"esptool {ESPTOOL_VERSION}（{os.path.dirname(getattr(esptool, '__file__', ''))}）",
        ]
        try:
            ports = list(list_ports.comports())
        except Exception as exc:
            lines.append(f"[错误] pyserial 枚举串口失败：{exc}")
            ports = []
        lines.append(f"① pyserial comports() 枚举到 {len(ports)} 个串口")
        for p in ports:
            vp = f"{p.vid:04X}:{p.pid:04X}" if p.vid and p.pid else "—"
            lines.append(f"     {p.device:<8} {p.description or ''}  [{vp}]  hwid={p.hwid}")

        if sys.platform == "win32":
            lines.extend(self._windows_port_report())
        else:
            lines.append("② 非 Windows 平台：请确认当前用户对 /dev/ttyUSB*、/dev/ttyACM* 有权限"
                         "（可执行 ls -l /dev/tty* 检查）")

        lines.append("③ 排查建议：数据线→驱动→设备管理器“端口(COM 和 LPT)”；"
                     "若显示“其他设备”带黄色叹号说明驱动未安装；"
                     "若端口被其它程序（串口助手/监视器）占用会提示“串口被占用”。")
        return lines

    def _windows_port_report(self) -> list[str]:
        """读取注册表，列出系统已知的串口与已登记但当前不在线的 USB 串口设备。"""
        lines: list[str] = []
        try:
            import winreg
        except ImportError:  # pragma: no cover
            lines.append("② 无法导入 winreg，跳过注册表检查")
            return lines

        def subkeys(root, path):
            out = []
            try:
                with winreg.OpenKey(root, path) as key:
                    index = 0
                    while True:
                        try:
                            out.append(winreg.EnumKey(key, index))
                        except OSError:
                            break
                        index += 1
            except OSError:
                pass
            return out

        def value(root, path, name):
            try:
                with winreg.OpenKey(root, path) as key:
                    return winreg.QueryValueEx(key, name)[0]
            except OSError:
                return None

        # 系统当前活动的串口映射
        active = {}
        path = r"HARDWARE\DEVICEMAP\SERIALCOMM"
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
                index = 0
                while True:
                    try:
                        name, dev, _ = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    active[str(dev).upper()] = name
                    index += 1
        except OSError as exc:
            lines.append(f"② 读取注册表 SERIALCOMM 失败：{exc}")
        lines.append(f"② 注册表 SERIALCOMM（系统当前活动串口）= "
                     f"{len(active)} 个" + (f"：{', '.join(sorted(active))}" if active else ""))

        # 已登记的 USB 串口设备（含已拔出/驱动异常的“幽灵”口）
        root_path = r"SYSTEM\CurrentControlSet\Enum\USB"
        registered = []
        for vidpid in subkeys(winreg.HKEY_LOCAL_MACHINE, root_path):
            for instance in subkeys(winreg.HKEY_LOCAL_MACHINE, f"{root_path}\\{vidpid}"):
                port_name = value(winreg.HKEY_LOCAL_MACHINE,
                                  f"{root_path}\\{vidpid}\\{instance}\\Device Parameters",
                                  "PortName")
                if port_name:
                    registered.append((str(port_name).upper(), vidpid, instance))
        if registered:
            lines.append(f"   已登记的 USB 串口设备 {len(registered)} 个：")
            for port_name, vidpid, _instance in sorted(registered):
                state = "活动" if port_name in active else "未在线（已拔出/驱动异常）"
                lines.append(f"     {port_name:<8} {vidpid:<26} [{state}]")
            if not active and registered:
                lines.append("   ⚠ 结论：系统曾安装过上述串口，但当前没有任何串口在线 —— "
                             "设备未连接、未被系统识别，或使用了仅充电的数据线。")
        else:
            lines.append("   未发现任何已登记的 USB 串口设备 —— "
                         "说明本机从未成功识别过 USB 转串口设备（通常是驱动未安装）。")
        return lines

    def _auto_refresh_tick(self) -> None:
        if self.auto_refresh.get() and not self.busy:
            self._refresh_ports()
        self.after(2500, self._auto_refresh_tick)

    def _on_port_selected(self) -> None:
        dev = self.var_port.get()
        row = self.port_rows.get(dev)
        if row and not self.busy:
            self.lbl_detail.configure(text=f"已选择 {dev}：{row['status']}")

    # ------------------------------------------------------------- 端口探测
    def _selected_port(self) -> str | None:
        dev = self.var_port.get().strip()
        if dev:
            return dev
        sel = self.tree_ports.selection()
        if sel:
            return self.tree_ports.item(sel[0], "values")[0]
        return None

    def _probe_port(self, port: str, attempts: int = 3):
        """连接指定串口并返回 (esp 对象, 芯片型号)。调用方负责关闭串口。"""
        esp = detect_chip(port=port, baud=115200,
                          connect_mode=self._opt("before", "default-reset"),
                          connect_attempts=attempts)
        return esp, esp.CHIP_NAME

    def _close_esp(self, esp) -> None:
        if esp is None:
            return
        try:
            esp._port.close()
        except Exception:
            pass

    def _disconnect_silent(self) -> None:
        if self.esp is not None:
            self._close_esp(self.esp)
            self.esp = None
        self.conn_port = None
        self.conn_chip = None

    def _disconnect_clicked(self) -> None:
        if self.esp is not None:
            self._disconnect_silent()
            self._log("info", "已断开设备连接")
        self._set_status("未连接", "idle", "就绪")

    def _apply_probe(self, ev: dict) -> None:
        dev = ev["port"]
        row = self.port_rows.get(dev)
        if not row:
            return
        row["chip"] = ev.get("chip", "—")
        row["status"] = ev.get("status", "")
        tag = ev.get("tag", "")
        self.tree_ports.item(row["iid"], values=(dev, row["chip"], row["status"], row["desc"]),
                             tags=(tag,) if tag else ())
        self.cmb_port.configure(values=list(self.port_rows.keys()))

    def _scan_devices(self, retry: bool = False) -> None:
        if ESPTOOL_IMPORT_ERROR is not None:
            messagebox.showerror(APP_NAME, f"esptool 不可用：\n{ESPTOOL_IMPORT_ERROR}")
            return
        if not self.port_rows:
            # 刷新一次再试；仍然没有端口就明确提示，避免无限重试
            if not retry:
                self._refresh_ports()
                self.after(400, lambda: self._scan_devices(retry=True))
                return
            self._log("warn", "没有可用串口，无法扫描设备。")
            messagebox.showwarning(
                APP_NAME,
                "没有检测到任何串口，无法扫描设备。\n\n"
                "请检查：\n"
                "  1. USB 线是否为数据线（不是充电线），并已插紧；\n"
                "  2. 串口驱动是否安装（CH340 / CP210x / FTDI / 原生 USB-JTAG）；\n"
                "  3. 设备管理器“端口(COM 和 LPT)”下是否有 COM 口。\n\n"
                "可以点击“端口诊断”查看系统层面的详细信息。")
            return
        self._disconnect_silent()
        self._start_worker("扫描设备", self._task_scan_devices)

    def _task_scan_devices(self) -> None:
        ports = list(self.port_rows.keys())
        self._emit_log("info", f"开始扫描 {len(ports)} 个串口…")
        for dev in ports:
            if self.cancel_flag:
                self._emit_log("warn", "扫描已中止")
                break
            self._emit("probe", port=dev, chip="…", status="检测中…", tag="busy")
            if DEMO_MODE:
                time.sleep(0.4)
                if dev in DEMO_CHIPS:
                    self._emit("probe", port=dev, chip=DEMO_CHIPS[dev], status="已识别", tag="ok")
                else:
                    self._emit("probe", port=dev, chip="—",
                               status=DEMO_BAD_PORTS.get(dev, "无响应"), tag="err")
                continue
            esp = None
            try:
                esp, chip = self._probe_port(dev, attempts=2)
                self._emit("probe", port=dev, chip=chip, status="已识别", tag="ok")
            except SystemExit:
                self._emit("probe", port=dev, chip="—", status="无响应", tag="err")
            except Exception as exc:
                self._emit("probe", port=dev, chip="—",
                           status=friendly_port_error(exc), tag="err")
                self._emit_log("warn", f"{dev}: {friendly_port_error(exc)}（{exc}）")
            finally:
                self._close_esp(esp)
        self._emit("status", text="扫描完成", kind="idle", detail="就绪")

    # ------------------------------------------------------------- 连接设备
    def _connect_device(self) -> None:
        if ESPTOOL_IMPORT_ERROR is not None:
            messagebox.showerror(APP_NAME, f"esptool 不可用：\n{ESPTOOL_IMPORT_ERROR}")
            return
        port = self._selected_port()
        if not port:
            messagebox.showwarning(APP_NAME, "请先选择串口（可点击“刷新端口”）。")
            return
        self._start_worker("连接设备", lambda: self._task_connect(port))

    def _open_device(self, port: str, initial_baud: int = 115200):
        """连接 + 可选加载 stub + 可选提升波特率 + attach flash（仅在工作线程中调用）。"""
        esp, chip = self._probe_port(port, attempts=3)
        if self._opt("stub", True) and not esp.secure_download_mode:
            esp = run_stub(esp)
        target_baud = int(self._opt("baud", initial_baud))
        if target_baud and target_baud != initial_baud and not esp.secure_download_mode:
            try:
                esp.change_baud(target_baud)
            except Exception as exc:
                self._emit_log("warn", f"无法切换到 {target_baud} bps，继续使用 "
                                       f"{esp._port.baudrate} bps（{exc}）")
        if not esp.secure_download_mode:
            attach_flash(esp)
        return esp

    def _task_connect(self, port: str) -> None:
        self._disconnect_silent()
        self._emit("progress_reset")
        self._emit("status", text=f"正在连接 {port}…", kind="busy", detail="")
        if DEMO_MODE:
            time.sleep(0.6)
            rows, caps, meta = self._demo_info(port)
            self.esp = object()
            self.conn_port = port
            self.conn_chip = meta.get("chip")
            self._emit("device", rows=rows, caps=caps, meta=meta)
            self._emit("status", text=f"已连接 {DEMO_CHIPS.get(port, 'ESP32')} @ {port}",
                       kind="ok", detail=f"{port}  ·  Stub 模式")
            self._emit_log("ok", f"演示模式：{port} 连接成功")
            return
        esp = None
        try:
            esp = self._open_device(port)
            self.esp = esp
            self.conn_port = port
            self.conn_baud = esp._port.baudrate
            rows, caps, meta = self._gather_info(esp, port)
            self.conn_chip = meta.get("chip")
            self._emit("device", rows=rows, caps=caps, meta=meta)
            self._emit("status", text=f"已连接 {esp.CHIP_NAME} @ {port}", kind="ok",
                       detail=f"{port} · {self.conn_baud} bps · "
                              f"{'Stub' if esp.IS_STUB else 'ROM'}")
            self._emit_log("ok", f"已连接 {esp.CHIP_NAME}（{port}）")
        except SystemExit as exc:
            self._emit_log("err", f"连接失败（esptool 终止）: {exc}")
            self._emit("status", text="连接失败", kind="err", detail=friendly_port_error(Exception(str(exc))))
        except Exception as exc:
            self._close_esp(esp)
            self.esp = None
            self._emit_log("err", f"连接 {port} 失败: {exc}")
            self._emit("status", text=f"连接失败：{friendly_port_error(exc)}", kind="err",
                       detail=str(exc)[:120])
            self._emit("device", rows=[("— 错误 —", f"{friendly_port_error(exc)}\n{exc}")], caps=None)

    # ------------------------------------------------------- 设备信息采集
    def _gather_info(self, esp, port: str):
        rows: list[tuple[str, str]] = []

        def add(item, value, tag=""):
            rows.append((item, str(value), tag))

        def safe(fn, default="（未知）"):
            try:
                return fn()
            except SystemExit:
                return default
            except NotSupportedError:
                return "（此芯片不支持）"
            except NotImplementedError:
                return "（此芯片不支持）"
            except Exception as exc:
                return f"读取失败: {exc}"

        def crystal_line():
            if not getattr(esp, "IS_STUB", False):
                return "（需 Stub 引导）"
            mhz = esp.get_crystal_freq()      # 返回值单位是 MHz
            return f"{mhz} MHz" if mhz else "（未能测量）"

        add("— 芯片 —", "")
        add("芯片型号", esp.CHIP_NAME)
        add("芯片描述", safe(esp.get_chip_description))
        try:
            major, minor = esp.get_major_chip_version(), esp.get_minor_chip_version()
            add("芯片版本", f"v{major}.{minor}")
        except Exception as exc:
            add("芯片版本", f"（未知: {exc}）")
        add("芯片特性", safe(lambda: " / ".join(esp.get_chip_features())))
        add("芯片 ID", safe(lambda: f"{esp.chip_id():#010x}"))
        add("MAC 地址", safe(lambda: mac_to_str(esp.read_mac("BASE_MAC"))))
        add("运行模式", "Stub 引导程序（加速）" if getattr(esp, "IS_STUB", False) else "ROM 引导程序")
        add("晶振频率", safe(crystal_line))

        add("— Flash —", "")
        flash_size_value = None
        try:
            flash_id = esp.flash_id()
            vendor_id = flash_id & 0xFF
            device_id = ((flash_id >> 16) & 0xFF) | (((flash_id >> 8) & 0xFF) << 8)
            add("Flash 厂商", f"{FLASH_VENDORS.get(vendor_id, '未知')} (0x{vendor_id:02X})")
            add("Flash 器件 ID", f"0x{device_id:04X}   原始 ID: 0x{flash_id:06X}")
            if esp.secure_download_mode:
                add("Flash 容量", "（安全下载模式下需手动指定）")
            else:
                size = safe(lambda: detect_flash_size(esp))
                if isinstance(size, str) and size and not size.startswith("读取失败"):
                    flash_size_value = size
                add("Flash 容量", size if isinstance(size, str) and size else "（未知）")
            add("Flash 电压", safe(esp.get_flash_voltage))
        except Exception as exc:
            add("Flash 信息", f"读取失败: {exc}", "warn")

        add("— 安全状态 —", "")
        add("安全启动", safe(lambda: "已启用" if esp.get_secure_boot_enabled() else "未启用"))
        add("Flash 加密", safe(lambda: "已启用" if esp.get_flash_encryption_enabled() else "未启用"))
        add("安全下载模式", "已启用" if esp.secure_download_mode else "未启用",
            "warn" if esp.secure_download_mode else "")
        add("USB 模式", safe(esp.get_usb_mode))

        add("— 连接 —", "")
        add("串口", port)
        add("波特率", f"{esp._port.baudrate} bps")
        add("终端 VID:PID", safe(lambda: "%04X:%04X" % esp.get_usb_vid_pid()))
        meta = {"chip": esp.CHIP_NAME, "flash_size": flash_size_value}
        return rows, getattr(esp, "FLASH_SIZES", None), meta

    def _demo_info(self, port: str):
        chip = DEMO_CHIPS.get(port, "ESP32-S3")
        rows = [
            ("— 芯片 —", ""),
            ("芯片型号", chip, ""),
            ("芯片描述", "Wi-Fi 6 & BLE 5.0, Dual Core + LP Core, 240MHz" if chip.startswith("ESP32-S3")
             else "Wi-Fi 6 & BLE 5.0, RISC-V single core, 160MHz", ""),
            ("芯片版本", "v0.2", ""),
            ("芯片特性", "Wi-Fi / BT / BLE / IEEE802.15.4 / Embedded Flash 8MB" if chip.startswith("ESP32-C3")
             else "Wi-Fi / BT / BLE / Embedded PSRAM 8MB", ""),
            ("芯片 ID", "0x00001234", ""),
            ("MAC 地址", "84:CC:A8:12:34:56", ""),
            ("运行模式", "Stub 引导程序（加速）", ""),
            ("晶振频率", "40 MHz", ""),
            ("— Flash —", ""),
            ("Flash 厂商", "GigaDevice (0xC8)", ""),
            ("Flash 器件 ID", "0x4017   原始 ID: 0xC84017", ""),
            ("Flash 容量", "8MB", ""),
            ("Flash 电压", "3.3V", ""),
            ("— 安全状态 —", ""),
            ("安全启动", "未启用", ""),
            ("Flash 加密", "未启用", ""),
            ("安全下载模式", "未启用", ""),
            ("USB 模式", "USB-Serial/JTAG", ""),
            ("— 连接 —", ""),
            ("串口", port, ""),
            ("波特率", "921600 bps", ""),
            ("终端 VID:PID", "303A:1001", ""),
        ]
        caps = {"keep": 0, "detect": 1, "256KB": 2, "512KB": 3, "1MB": 4, "2MB": 5,
                "4MB": 6, "8MB": 7, "16MB": 8, "32MB": 9, "64MB": 10, "128MB": 11}
        meta = {"chip": chip, "flash_size": "8MB"}
        return rows, caps, meta

    def _show_device_rows(self, rows) -> None:
        self.device_rows = [(r[0], r[1]) for r in rows]
        for item in self.tree_info.get_children():
            self.tree_info.delete(item)
        for row in rows:
            item, value = row[0], row[1]
            tag = row[2] if len(row) > 2 else ""
            if not tag:
                if item.startswith("—"):
                    tag = "section"
                elif value in ("已启用",):
                    tag = "warn"
                elif value == "未启用":
                    tag = "ok"
            self.tree_info.insert("", "end", values=(item, value), tags=(tag,) if tag else ())

    def _apply_flash_caps(self, caps) -> None:
        if not caps:
            return
        keys = list(caps.keys())
        if "keep" in keys and self.var_flash_size.get() not in keys:
            self.cmb_flash_size.configure(values=keys)

    def _copy_info(self) -> None:
        if not self.device_rows:
            return
        text = "\n".join(f"{k}\t{v}" for k, v in self.device_rows if v)
        self.clipboard_clear()
        self.clipboard_append(text)
        self._log("ok", "设备信息已复制到剪贴板")

    def _reload_info(self) -> None:
        port = self.conn_port or self._selected_port()
        if not port:
            messagebox.showwarning(APP_NAME, "请先选择串口。")
            return
        self._start_worker("读取设备信息", lambda: self._task_connect(port))

    # ------------------------------------------------------------- 烧录相关
    def _update_flash_mode(self) -> None:
        for frame in (self.frame_single, self.frame_multi):
            frame.pack_forget()
        before = getattr(self, "opts_frame", None)
        if self.var_mode.get() == "single":
            self.frame_single.pack(fill="x", pady=6, before=before)
        else:
            self.frame_multi.pack(fill="both", expand=True, pady=6, before=before)

    def _pick_single_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择合并后的固件镜像",
            filetypes=[("固件镜像", "*.bin"), ("所有文件", "*.*")])
        if path:
            self.var_single_file.set(path)

    def _suggest_offset(self, path: str) -> str:
        name = os.path.basename(path).lower()
        if "bootloader" in name:
            return "0x0"
        if "partition" in name:
            return "0x8000"
        if "boot_app0" in name:
            return "0xe000"
        if "ota" in name or "app" in name:
            return "0x10000"
        if "spiffs" in name or "littlefs" in name:
            return "0x210000"
        return "0x0"

    def _add_flash_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="选择要烧录的固件文件",
            filetypes=[("固件文件", "*.bin"), ("所有文件", "*.*")])
        for path in paths:
            offset = self._suggest_offset(path)
            try:
                size = human_size(os.path.getsize(path))
            except OSError:
                size = "?"
            self.tree_files.insert("", "end", values=(path, offset, size))

    def _remove_flash_files(self) -> None:
        for iid in self.tree_files.selection():
            self.tree_files.delete(iid)

    def _clear_flash_files(self) -> None:
        for iid in self.tree_files.get_children():
            self.tree_files.delete(iid)

    def _edit_file_offset(self, _event=None) -> None:
        sel = self.tree_files.selection()
        if not sel:
            return
        iid = sel[0]
        cur = self.tree_files.item(iid, "values")[1]
        new = simpledialog.askstring("修改烧录地址", "请输入烧录地址（十六进制，如 0x10000）：",
                                     initialvalue=cur, parent=self)
        if new:
            try:
                int(new, 0)
            except ValueError:
                messagebox.showerror(APP_NAME, "地址格式不正确，请输入如 0x10000 的十六进制数。")
                return
            values = list(self.tree_files.item(iid, "values"))
            values[1] = new
            self.tree_files.item(iid, values=values)

    def _collect_flash_targets(self):
        if self.var_mode.get() == "single":
            path = self.var_single_file.get().strip()
            if not path:
                raise ValueError("请先选择固件镜像文件。")
            if not DEMO_MODE and not os.path.isfile(path):
                raise ValueError(f"文件不存在：{path}")
            return [(int(self.var_single_addr.get().strip() or "0x0", 0), path)]
        items = self.tree_files.get_children()
        if not items:
            raise ValueError("请先添加至少一个固件文件。")
        targets = []
        for iid in items:
            path, offset, _size = self.tree_files.item(iid, "values")
            if not DEMO_MODE and not os.path.isfile(path):
                raise ValueError(f"文件不存在：{path}")
            try:
                addr = int(str(offset), 0)
            except ValueError:
                raise ValueError(f"地址格式错误：{path} -> {offset}")
            targets.append((addr, path))
        return targets

    def _ensure_esp(self, port: str):
        if self.esp is not None and self.conn_port == port:
            return self.esp
        self._disconnect_silent()
        esp = self._open_device(port)
        self.esp = esp
        self.conn_port = port
        self.conn_baud = esp._port.baudrate
        return esp

    def _start_flash(self) -> None:
        if ESPTOOL_IMPORT_ERROR is not None:
            messagebox.showerror(APP_NAME, f"esptool 不可用：\n{ESPTOOL_IMPORT_ERROR}")
            return
        port = self._selected_port()
        if not port:
            messagebox.showwarning(APP_NAME, "请先选择串口。")
            return
        try:
            targets = self._collect_flash_targets()
        except ValueError as exc:
            messagebox.showwarning(APP_NAME, str(exc))
            return
        total = sum(os.path.getsize(p) for _a, p in targets)
        desc = "\n".join(f"  {a:#010x}  {p}" for a, p in targets)
        if not messagebox.askokcancel(
                APP_NAME,
                f"即将向 {port} 烧录 {len(targets)} 个文件，共 {human_size(total)}：\n\n{desc}\n\n确认继续？"):
            return
        self._start_worker("烧录固件", lambda: self._task_flash(port, targets))

    def _task_flash(self, port: str, targets) -> None:
        self._emit("progress_reset")
        self._emit("status", text="正在烧录…", kind="busy", detail="")
        if DEMO_MODE:
            time.sleep(0.4)
            for i in range(0, 101, 4):
                if self.cancel_flag:
                    break
                self._emit("progress", cur=i, total=100)
                time.sleep(0.03)
            self._emit_log("ok", "演示模式：烧录完成")
            self._emit("status", text="烧录完成（演示）", kind="ok", detail="")
            return
        esp = None
        try:
            esp = self._ensure_esp(port)
            write_flash(esp, targets,
                        flash_freq=self._opt("flash_freq", "keep"),
                        flash_mode=self._opt("flash_mode", "keep"),
                        flash_size=self._opt("flash_size", "keep"),
                        erase_all=self._opt("erase", False),
                        compress=self._opt("compress", True))
            if self._opt("verify", True):
                self._emit_status_note("正在校验…")
                verify_flash(esp, targets,
                             flash_freq=self._opt("flash_freq", "keep"),
                             flash_mode=self._opt("flash_mode", "keep"),
                             flash_size=self._opt("flash_size", "keep"))
            if self._opt("reset", True):
                reset_chip(esp, "hard-reset")
            self._emit_log("ok", "烧录完成")
            self._emit("status", text="烧录完成", kind="ok",
                       detail=f"{esp.CHIP_NAME} @ {port}")
        except SystemExit as exc:
            self._emit_log("err", f"烧录中止: {exc}")
            self._emit("status", text="烧录失败", kind="err", detail=str(exc)[:120])
        except Exception as exc:
            self._emit_log("err", f"烧录失败: {exc}")
            self._emit_log("debug", traceback.format_exc())
            self._emit("status", text="烧录失败", kind="err", detail=str(exc)[:120])
            self._close_esp(esp)
            self.esp = None

    def _emit_status_note(self, text: str) -> None:
        self._emit("status", text=text, kind="busy", detail="")

    # ------------------------------------------------------------- 高级工具
    def _start_erase(self) -> None:
        if ESPTOOL_IMPORT_ERROR is not None:
            messagebox.showerror(APP_NAME, f"esptool 不可用：\n{ESPTOOL_IMPORT_ERROR}")
            return
        port = self._selected_port()
        if not port:
            messagebox.showwarning(APP_NAME, "请先选择串口。")
            return
        if not messagebox.askokcancel(APP_NAME, f"确定要擦除 {port} 上设备的整片 Flash 吗？\n"
                                                "此操作不可撤销，设备上的所有数据都会丢失。"):
            return
        self._start_worker("擦除 Flash", lambda: self._task_erase(port))

    def _task_erase(self, port: str) -> None:
        self._emit("progress_reset")
        self._emit("status", text="正在擦除 Flash…", kind="busy", detail="")
        if DEMO_MODE:
            time.sleep(1.0)
            self._emit_log("ok", "演示模式：擦除完成")
            self._emit("status", text="擦除完成（演示）", kind="ok", detail="")
            return
        esp = None
        try:
            esp = self._ensure_esp(port)
            erase_flash(esp, force=False)
            self._emit_log("ok", "擦除完成")
            self._emit("status", text="擦除完成", kind="ok", detail=f"{port}")
        except SystemExit as exc:
            self._emit_log("err", f"擦除中止: {exc}")
            self._emit("status", text="擦除失败", kind="err", detail=str(exc)[:120])
        except Exception as exc:
            self._emit_log("err", f"擦除失败: {exc}")
            self._close_esp(esp)
            self.esp = None
            self._emit("status", text="擦除失败", kind="err", detail=str(exc)[:120])

    def _pick_read_out(self) -> None:
        path = filedialog.asksaveasfilename(title="保存读取的 Flash 数据",
                                            defaultextension=".bin",
                                            filetypes=[("二进制文件", "*.bin"), ("所有文件", "*.*")])
        if path:
            self.var_read_out.set(path)

    def _start_read(self) -> None:
        if ESPTOOL_IMPORT_ERROR is not None:
            messagebox.showerror(APP_NAME, f"esptool 不可用：\n{ESPTOOL_IMPORT_ERROR}")
            return
        port = self._selected_port()
        if not port:
            messagebox.showwarning(APP_NAME, "请先选择串口。")
            return
        try:
            addr = int(self.var_read_addr.get().strip(), 0)
            size = int(self.var_read_size.get().strip(), 0)
        except ValueError:
            messagebox.showerror(APP_NAME, "地址或长度格式不正确（示例：0x0 / 0x100000）。")
            return
        out = self.var_read_out.get().strip()
        if not out:
            self._pick_read_out()
            out = self.var_read_out.get().strip()
        if not out:
            return
        self._start_worker("读取 Flash", lambda: self._task_read(port, addr, size, out))

    def _task_read(self, port: str, addr: int, size: int, out: str) -> None:
        self._emit("progress_reset")
        self._emit("status", text="正在读取 Flash…", kind="busy", detail="")
        if DEMO_MODE:
            time.sleep(1.0)
            self._emit_log("ok", f"演示模式：已保存到 {out}")
            self._emit("status", text="读取完成（演示）", kind="ok", detail=out)
            return
        esp = None
        try:
            esp = self._ensure_esp(port)
            size_opt = self._opt("flash_size", "keep")
            read_flash(esp, addr, size, output=out,
                       flash_size=size_opt if size_opt != "keep" else "detect")
            self._emit_log("ok", f"已读取 {human_size(size)} 并保存到 {out}")
            self._emit("status", text="读取完成", kind="ok", detail=out)
        except SystemExit as exc:
            self._emit_log("err", f"读取中止: {exc}")
            self._emit("status", text="读取失败", kind="err", detail=str(exc)[:120])
        except Exception as exc:
            self._emit_log("err", f"读取失败: {exc}")
            self._close_esp(esp)
            self.esp = None
            self._emit("status", text="读取失败", kind="err", detail=str(exc)[:120])

    def _start_reset(self) -> None:
        port = self._selected_port()
        if not port:
            messagebox.showwarning(APP_NAME, "请先选择串口。")
            return
        self._start_worker("复位设备", lambda: self._task_reset(port))

    def _task_reset(self, port: str) -> None:
        if DEMO_MODE:
            time.sleep(0.3)
            self._emit_log("ok", "演示模式：复位完成")
            return
        esp = None
        try:
            esp = self._ensure_esp(port)
            reset_chip(esp, self._opt("reset_mode", "hard-reset"))
            self._emit_log("ok", "复位指令已发送")
            self._emit("status", text="已复位", kind="ok", detail=port)
        except Exception as exc:
            self._emit_log("err", f"复位失败: {exc}")
            self._emit("status", text="复位失败", kind="err", detail=str(exc)[:120])

    def _start_flash_id(self) -> None:
        port = self._selected_port()
        if not port:
            messagebox.showwarning(APP_NAME, "请先选择串口。")
            return
        self._start_worker("读取 Flash ID", lambda: self._task_flash_id(port))

    def _task_flash_id(self, port: str) -> None:
        if DEMO_MODE:
            self._emit_log("ok", "演示模式：Flash ID = 0xC84017")
            return
        esp = None
        try:
            esp = self._ensure_esp(port)
            fid = esp.flash_id()
            vendor = fid & 0xFF
            self._emit_log("ok", f"Flash ID: 0x{fid:06X}  厂商: "
                                 f"{FLASH_VENDORS.get(vendor, '未知')} (0x{vendor:02X})")
        except Exception as exc:
            self._emit_log("err", f"读取 Flash ID 失败: {exc}")

    def _start_read_mac(self) -> None:
        port = self._selected_port()
        if not port:
            messagebox.showwarning(APP_NAME, "请先选择串口。")
            return
        self._start_worker("读取 MAC", lambda: self._task_read_mac(port))

    def _task_read_mac(self, port: str) -> None:
        if DEMO_MODE:
            self._emit_log("ok", "演示模式：MAC = 84:CC:A8:12:34:56")
            return
        esp = None
        try:
            esp = self._ensure_esp(port)
            self._emit_log("ok", f"MAC: {mac_to_str(esp.read_mac('BASE_MAC'))}")
        except Exception as exc:
            self._emit_log("err", f"读取 MAC 失败: {exc}")

    # ------------------------------------------------------- 编译：环境与任务
    def _require_compile(self) -> bool:
        if esp_compile is None:
            messagebox.showerror(APP_NAME, f"编译模块不可用：\n{COMPILE_IMPORT_ERROR}")
            return False
        return True

    def _log_line(self, text: str) -> None:
        """外部命令输出的逐行回调（在工作线程中被调用）。"""
        self._emit_log("info", text)

    def _start_env_check(self) -> None:
        if not self._require_compile():
            return
        self._start_worker("检测编译环境", self._task_env_check, blocking=False)

    def _task_env_check(self) -> None:
        report = esp_compile.environment_report()
        lines = esp_compile.describe_environment(report)
        self._emit("compile_env", lines=lines, cores=[c["id"] for c in report.get("cores") or []])
        for line in lines:
            self._emit_log("info", line)
        cli = report.get("arduino_cli")
        if not cli:
            self._emit_log("warn", "未找到 arduino-cli：可安装 Arduino IDE 2.x"
                                   "（其自带 arduino-cli），或单独安装 arduino-cli")
            return
        self._emit_log("ok", "Arduino 编译链可用，可直接编译 .ino / .cpp / .h")

        # 若有已连接设备，优先列出该芯片可用的板卡；否则列出主工具链的全部 ESP 板卡
        boards = self._board_values_for_current_target()
        if boards:
            self._emit("board_list", values=boards)
            self._emit_log("info", f"已列出 {len(boards)} 个可用开发板 FQBN")

        # 预热每个工具链的板卡缓存（board listall 要几秒），
        # 这样以后连接设备时界面不会卡顿
        for tc in esp_compile.detect_toolchains():
            for prefix in ("esp32", "esp8266"):
                if any(cid.startswith(prefix + ":") for cid in tc["cores"]):
                    try:
                        esp_compile.arduino_boards(tc["cli"], prefix,
                                                   config_args=tc["config_args"])
                    except Exception:
                        pass

    def _board_values_for_current_target(self) -> list[str]:
        """给下拉框用的 FQBN 列表：优先按已连接设备的芯片过滤。"""
        chip = esp_compile.chip_from_text(self.conn_chip) if self.conn_chip else None
        tc = esp_compile.resolve_toolchain(chip=chip) if chip else None
        if chip and tc:
            boards = esp_compile.boards_for_chip(chip, toolchain=tc)
            if boards:
                return boards
        if tc is None:
            tc = esp_compile.resolve_toolchain()
        if tc is None:
            return []
        found: list[str] = []
        for prefix in ("esp32", "esp8266"):
            if any(cid.startswith(prefix + ":") for cid in tc["cores"]):
                for _name, fqbn in esp_compile.arduino_boards(tc["cli"], prefix,
                                                              config_args=tc["config_args"]):
                    if fqbn not in found:
                        found.append(fqbn)
        return sorted(found)

    def _start_board_list(self) -> None:
        if not self._require_compile():
            return
        self._start_worker("获取开发板列表", self._task_board_list, blocking=False)

    def _task_board_list(self) -> None:
        fqbns = self._board_values_for_current_target()
        if not fqbns:
            raise RuntimeError("未找到可用的开发板列表（缺少 arduino-cli 或开发板核心）")
        self._emit("board_list", values=fqbns)
        chip = esp_compile.chip_from_text(self.conn_chip) if self.conn_chip else None
        if chip:
            self._emit_log("ok", f"已按已连接设备（{esp_compile.chip_display(chip)}）"
                                 f"列出 {len(fqbns)} 个可用开发板 FQBN")
        else:
            self._emit_log("ok", f"共获取 {len(fqbns)} 个 ESP 系列开发板 FQBN")

    def _start_compile(self) -> None:
        if not self._require_compile():
            return
        mode = self.var_compile_mode.get()
        if mode == "arduino":
            self._start_worker("编译 Arduino 工程", self._task_compile_arduino)
        elif mode == "idf":
            self._start_worker("编译 ESP-IDF 工程", self._task_compile_idf)
        elif mode == "elf":
            self._start_worker("ELF 转 BIN", self._task_elf2bin)
        else:
            self._start_worker("合并 BIN", self._task_merge_bin)

    def _task_compile_arduino(self) -> None:
        src = (self._opt("c_src") or "").strip()
        if not src or not os.path.isdir(src):
            raise RuntimeError("请选择有效的源码目录（包含 .ino / .cpp / .h）")
        fqbn = (self._opt("c_fqbn") or "esp32:esp32:esp32").strip()
        out_dir = (self._opt("c_out") or "").strip() or os.path.join(src, "build_out")
        chip = esp_compile.chip_from_fqbn(fqbn)

        # 按目标芯片选工具链：内置 C6 环境只有 RISC-V，系统 Arduino IDE 才有 Xtensa
        tc = esp_compile.resolve_toolchain(chip=chip, fqbn=fqbn)
        cli = tc["cli"] if tc else esp_compile.find_arduino_cli()
        if not cli:
            raise RuntimeError("未找到 arduino-cli。请安装 Arduino IDE 2.x，"
                               "或在 PATH 中加入 arduino-cli")
        config_args = tc["config_args"] if tc else None
        short_args = tc["short_tool_args"] if tc else None
        if tc:
            self._log_line(f"[信息] 编译环境：{tc['label']}"
                           f"（{'、'.join(f'{k} {v}' for k, v in tc['cores'].items())}）")
        if self.conn_chip and chip != esp_compile.chip_from_text(self.conn_chip):
            self._log_line(f"[警告] 所选开发板是 {esp_compile.chip_display(chip)}，"
                           f"而当前连接的设备是 {self.conn_chip}，烧录前请确认是否匹配")

        ok, why = esp_compile.check_fqbn(cli, fqbn, config_args=config_args)
        if not ok:
            cores = "、".join(f"{c['id']} {c['installed']}".strip()
                             for c in esp_compile.arduino_cores(cli, config_args=config_args)) or "（无）"
            hint = ""
            if chip not in ("esp32", "esp32s2", "esp32s3", "esp32c3", "esp8266"):
                hint = (f"\n提示：{esp_compile.chip_display(chip)} 需要较新的 esp32 核心"
                        f"（3.x 及以上），当前安装的核心版本可能不支持该芯片。")
            raise RuntimeError(f"开发板 FQBN 不可用：{fqbn}\n{why}\n"
                               f"该环境已安装的核心：{cores}{hint}\n"
                               f"可点“刷新开发板列表”选择本机真实存在的板卡。")

        self._emit("status", text="正在编译…", kind="busy", detail=f"{fqbn}")

        if not esp_compile.find_sources(src):
            raise RuntimeError(f"目录里没有可编译的源文件（.ino/.c/.cpp/.h）：{src}")

        sketch, _wrapped = esp_compile.wrap_sources_as_sketch(src, on_line=self._log_line)
        compile_log: list[str] = []

        def _capture(text: str) -> None:
            compile_log.append(text)
            self._emit_log("info", text)

        rc = esp_compile.build_arduino(cli, fqbn, sketch, out_dir,
                                       on_line=_capture,
                                       verbose=bool(self._opt("c_verbose")),
                                       config_args=config_args,
                                       short_tool_args=short_args)
        if rc != 0:
            missing = esp_compile.parse_missing_headers(compile_log)
            if missing:
                self._emit("lib_hint", header=missing[0])
                raise RuntimeError(
                    "编译失败：缺少头文件 " + "、".join(missing) +
                    "。很可能是没有安装对应的库 —— 已切到“库管理”页并填好关键字，"
                    "安装后重新编译即可。")
            raise RuntimeError(f"编译失败（arduino-cli 退出码 {rc}），详见运行日志")

        pkg_arch = ":".join(fqbn.split(":")[:2])
        data_dir = tc["data_dir"] if tc else esp_compile.arduino_data_dir()
        core_dir = esp_compile.core_install_dir(data_dir, pkg_arch)
        items = esp_compile.collect_arduino_items(out_dir, fqbn, core_dir, on_line=self._log_line)
        if not items:
            raise RuntimeError(f"编译成功但输出目录里没有 .bin：{out_dir}")

        merged = None
        if self._opt("c_merge"):
            merged = os.path.join(out_dir, "factory.bin")
            esp_compile.merge_bins(items, chip, merged, on_line=self._log_line)
        self._emit_compiled(items, merged, out_dir)

    def _task_compile_idf(self) -> None:
        src = (self._opt("i_src") or "").strip()
        if not src or not os.path.isdir(src):
            raise RuntimeError("请选择有效的 ESP-IDF 工程目录")
        if not os.path.isfile(os.path.join(src, "CMakeLists.txt")):
            self._log_line("[警告] 工程目录下没有 CMakeLists.txt，可能不是 ESP-IDF 工程")
        idf = esp_compile.find_idf()
        if not idf:
            raise RuntimeError("未找到 ESP-IDF。请安装 ESP-IDF（需要 export.bat）"
                               "或设置 IDF_PATH 环境变量")
        chip = (self._opt("i_target") or "esp32").strip() or "esp32"
        build_dir = os.path.join(src, "build")
        self._emit("status", text="正在执行 idf.py build…", kind="busy", detail=chip)

        rc = esp_compile.build_idf(src, build_dir, idf, target=chip, on_line=self._log_line)
        if rc != 0:
            raise RuntimeError(f"idf.py build 失败（退出码 {rc}），详见运行日志")

        items = esp_compile.collect_idf_items(build_dir, os.path.basename(os.path.normpath(src)),
                                              chip, on_line=self._log_line)
        if not items:
            raise RuntimeError(f"没有在 {build_dir} 找到可烧录的 .bin")
        merged = None
        if self._opt("i_merge"):
            merged = os.path.join(build_dir, "factory.bin")
            esp_compile.merge_bins(items, chip, merged, on_line=self._log_line)
        self._emit_compiled(items, merged, build_dir)

    def _task_elf2bin(self) -> None:
        elf = (self._opt("e_elf") or "").strip()
        if not elf or not os.path.isfile(elf):
            raise RuntimeError("请选择有效的 ELF 文件")
        out = (self._opt("e_out") or "").strip() or os.path.splitext(elf)[0] + ".bin"
        chip = self._opt("e_chip") or "esp32"
        self._emit("status", text="正在转换 ELF → BIN…", kind="busy", detail=chip)
        esp_compile.elf_to_bin(elf, chip, out,
                               flash_mode=self._opt("e_mode") or "dio",
                               flash_size=self._opt("e_size") or "4MB",
                               flash_freq=self._opt("e_freq") or "40m")
        if not os.path.isfile(out):
            raise RuntimeError("转换失败，没有生成输出文件")
        self._log_line(f"[完成] 已生成 {out}（{os.path.getsize(out)} 字节）")
        addr = "0x0" if chip == "esp8266" else "0x10000"
        self._log_line(f"[提示] 应用镜像通常烧录到 {addr}，已按此填入烧录页，如有需要可自行修改")
        self._emit_compiled([(out, addr)], None, os.path.dirname(out))

    def _task_merge_bin(self) -> None:
        rows = self._opt("m_items") or []
        if not rows:
            raise RuntimeError("请先添加至少一个 bin 文件")
        items = []
        for addr_text, path in rows:
            if not os.path.isfile(path):
                raise RuntimeError(f"文件不存在：{path}")
            items.append((esp_compile.parse_addr(addr_text), path))
        out = (self._opt("m_out") or "").strip()
        if not out:
            out = os.path.join(os.path.dirname(items[0][1]), "factory.bin")
        chip = self._opt("m_chip") or "esp32"
        self._emit("status", text="正在合并 BIN…", kind="busy", detail=chip)
        esp_compile.merge_bins(items, chip, out, on_line=self._log_line)
        self._emit_compiled(items, out, os.path.dirname(out))

    def _emit_compiled(self, items, merged, out_dir) -> None:
        payload = []
        for addr, path in items:
            addr_text = addr if isinstance(addr, str) else f"{addr:#x}"
            try:
                size = human_size(os.path.getsize(path))
            except OSError:
                size = "?"
            payload.append((path, addr_text, size))
        self._emit("compiled", items=payload, merged=merged, out_dir=out_dir)

    def _apply_compiled(self, ev: dict) -> None:
        """编译完成：把产物填进“烧录固件”页。"""
        merged = ev.get("merged")
        items = ev.get("items") or []
        if merged and os.path.isfile(merged):
            self.var_mode.set("single")
            self.var_single_file.set(merged)
            self.var_single_addr.set("0x0")
            self._update_flash_mode()
            self._log("ok", f"已把 {os.path.basename(merged)} 填入“烧录固件”页"
                            f"（单个合并镜像 @0x0，{human_size(os.path.getsize(merged))}）")
        elif items:
            self.var_mode.set("multi")
            self._clear_flash_files()
            for path, addr, size in items:
                self.tree_files.insert("", "end", values=(path, addr, size))
            self._update_flash_mode()
            self._log("ok", f"已把 {len(items)} 个编译产物填入“烧录固件”页")
        self._set_status("编译完成", "ok", "产物已填入烧录页，可直接点“开始烧录”")

    # ------------------------------------------------- 按已连接设备自动匹配
    def _on_auto_match_toggled(self) -> None:
        if self.var_auto_match.get():
            if self.conn_chip:
                self._auto_match_device(self.conn_chip, source="重新启用自动匹配")
            else:
                self.lbl_match.configure(text="自动匹配已开启：连接设备后会自动选择芯片与开发板")
        else:
            self.lbl_match.configure(text="自动匹配已关闭：编译设置完全由手动指定")

    def _match_connected(self) -> None:
        if not self.conn_chip:
            self._log("warn", "尚未连接设备：请先在左侧选择串口并点“连接 / 识别”")
            self.lbl_match.configure(text="尚未连接设备：连接后会自动匹配芯片与开发板")
            messagebox.showinfo(APP_NAME, "尚未连接设备。\n请先选择串口并点击“连接 / 识别”。")
            return
        self._auto_match_device(self.conn_chip, source="手动匹配")

    def _auto_match_device(self, chip_name: str, flash_size: str | None = None,
                           source: str = "连接设备") -> None:
        """按已连接设备的芯片自动选择：工具链、编译方式、开发板、各页芯片与 Flash 大小。"""
        if esp_compile is None:
            return
        chip = esp_compile.chip_from_text(chip_name)
        if not chip:
            self._log("warn", f"无法从“{chip_name}”识别芯片型号，未自动匹配")
            return
        shown = esp_compile.chip_display(chip) if hasattr(esp_compile, "chip_display") else chip.upper()
        self._log("note", f"── 按已连接设备自动匹配（{source}）：{shown} ──")

        # 1) 工具链：本机可能存在多个 Arduino 环境，各自支持的芯片不同
        tc = esp_compile.resolve_toolchain(chip=chip)
        if tc is None:
            self._log("err", "未找到任何 Arduino 工具链，无法编译")
            self.lbl_match.configure(text=f"{shown}：未找到可用的编译工具链")
            return
        if chip in tc["chips"]:
            self._log("ok", f"选用编译环境：{tc['label']}"
                            f"（{'、'.join(f'{k} {v}' for k, v in tc['cores'].items())}）")
            tool_line = f"{tc['label']}"
        else:
            others = "；".join(esp_compile.describe_toolchains())
            self._log("warn", f"本机没有能编译 {shown} 的工具链（需要对应版本的 esp32 核心）。"
                              f"当前探测结果：{others}")
            tool_line = "无可用工具链"

        # 2) 编译方式：按源码目录的工程类型判断
        mode_line = ""
        src = self.var_c_src.get().strip()
        if src and os.path.isdir(src):
            if os.path.isfile(os.path.join(src, "CMakeLists.txt")):
                mode = "idf"
                why = "检测到 CMakeLists.txt"
            elif os.path.isfile(os.path.join(src, "platformio.ini")):
                mode = "arduino"
                why = "检测到 platformio.ini（PlatformIO 配置不会被解析，按 Arduino 源码编译）"
            else:
                mode = "arduino"
                why = "检测到 C/C++ 源码"
            if self.var_compile_mode.get() != mode:
                self.var_compile_mode.set(mode)
                self._update_compile_mode()
            mode_line = f"{why} → {mode}"
            self._log("info", f"编译方式：{why} → {'ESP-IDF 工程' if mode == 'idf' else 'Arduino 工程'}")

        # 3) 开发板 FQBN
        board_line = ""
        boards = esp_compile.boards_for_chip(chip, toolchain=tc)
        if boards:
            current = self.var_c_fqbn.get().strip()
            if current not in boards:
                self.var_c_fqbn.set(boards[0])
                self._log("ok", f"自动选择开发板：{boards[0]}（该芯片共 {len(boards)} 块可选）")
            else:
                self._log("info", f"开发板已是 {current}，保持不变")
            self.cmb_fqbn.configure(values=boards)
            board_line = boards[0]
        else:
            self._log("warn", f"该环境中没有 {shown} 的开发板可选项")
            board_line = "无匹配开发板"

        # 4) 其它页面的芯片选择
        for var in (self.var_i_target, self.var_e_chip, self.var_m_chip):
            var.set(chip)
        self._log("info", f"ESP-IDF 目标 / ELF→BIN / 合并 BIN 的芯片均设为 {chip}")

        # 5) Flash 大小按设备实测值填（仅当是一个已知档位）
        flash_line = ""
        if flash_size:
            known = list(self.cmb_flash_size.cget("values"))
            if flash_size in known:
                self.var_flash_size.set(flash_size)
                flash_line = flash_size
                self._log("ok", f"Flash 大小按设备实测值设为 {flash_size}")

        summary = "；".join(x for x in (
            f"芯片 {shown}",
            f"编译环境 {tool_line}" if tool_line else "",
            f"开发板 {board_line}" if board_line else "",
            f"编译方式 {mode_line}" if mode_line else "",
            f"Flash {flash_line}" if flash_line else "",
        ) if x)
        self.lbl_match.configure(text="已按已连接设备自动配置 —— " + summary)

    # ------------------------------------------------------------ 库管理页
    def _build_library_tab(self) -> None:
        top = ttk.Frame(self.tab_lib)
        top.pack(fill="x")
        ttk.Label(top, text="库安装目录:", font=self.font_ui_bold).pack(side="left")
        self.var_lib_dir = tk.StringVar(value="（点击“刷新已安装库”或切换本页后自动检测）")
        ttk.Entry(top, textvariable=self.var_lib_dir, state="readonly").pack(
            side="left", fill="x", expand=True, padx=4)
        ttk.Button(top, text="打开目录", command=self._open_library_dir).pack(side="left")
        ttk.Button(top, text="更新库索引", command=self._start_lib_update_index).pack(side="left", padx=4)
        ttk.Button(top, text="升级全部库", command=self._start_lib_upgrade).pack(side="left")

        # --- 安装 ---
        inst = ttk.Labelframe(self.tab_lib, text=" 安装指定库 ", padding=8)
        inst.pack(fill="x", pady=(8, 0))
        row = ttk.Frame(inst)
        row.pack(fill="x")
        self.var_lib_source = tk.StringVar(value="name")
        for text, value in (("按名称（库管理器）", "name"), ("从 ZIP 文件", "zip"),
                            ("从 Git 仓库", "git"), ("从本地文件夹", "dir")):
            ttk.Radiobutton(row, text=text, value=value, variable=self.var_lib_source,
                            command=self._update_lib_source).pack(side="left", padx=(0, 12))

        row2 = ttk.Frame(inst)
        row2.pack(fill="x", pady=(6, 0))
        self.var_lib_input = tk.StringVar()
        self.entry_lib_input = ttk.Entry(row2, textvariable=self.var_lib_input)
        self.entry_lib_input.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.entry_lib_input.bind("<Return>", lambda _e: self._start_lib_install())
        self.btn_lib_browse = ttk.Button(row2, text="浏览…", command=self._pick_lib_source)
        self.btn_lib_browse.pack(side="left")
        ttk.Button(row2, text="安装", style="Accent.TButton",
                   command=self._start_lib_install).pack(side="left", padx=6)
        self.var_lib_nodeps = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2, text="不安装依赖", variable=self.var_lib_nodeps).pack(side="left", padx=6)

        self.lbl_lib_hint = ttk.Label(inst, style="Muted.TLabel", wraplength=self.px(980),
                                      justify="left", text=self._lib_source_hint())
        self.lbl_lib_hint.pack(anchor="w", pady=(6, 0))

        # --- 搜索 + 已安装：放在可拖动的上下分栏里，避免空间不够时把表格挤扁 ---
        self.lib_panes = ttk.Panedwindow(self.tab_lib, orient="vertical")
        self.lib_panes.pack(fill="both", expand=True, pady=(8, 0))

        search = ttk.Labelframe(self.lib_panes, text=" 搜索库（双击结果即可安装） ", padding=6)
        have = ttk.Labelframe(self.lib_panes, text=" 已安装库 ", padding=6)
        self.lib_panes.add(search, weight=1)
        self.lib_panes.add(have, weight=1)

        srow = ttk.Frame(search)
        srow.pack(fill="x")
        ttk.Label(srow, text="关键字:").pack(side="left")
        self.var_lib_query = tk.StringVar()
        entry_q = ttk.Entry(srow, textvariable=self.var_lib_query)
        entry_q.pack(side="left", fill="x", expand=True, padx=4)
        entry_q.bind("<Return>", lambda _e: self._start_lib_search())
        ttk.Button(srow, text="搜索", command=self._start_lib_search).pack(side="left")

        wrap_s = ttk.Frame(search)
        wrap_s.pack(fill="both", expand=True, pady=(6, 0))
        cols = ("name", "latest", "author", "sentence")
        self.tree_libsearch = ttk.Treeview(wrap_s, columns=cols, show="headings", height=3)
        for key, (text, width, stretch) in {
            "name": ("库名称", 210, False), "latest": ("最新版本", 80, False),
            "author": ("作者", 130, False), "sentence": ("说明", 380, True),
        }.items():
            self.tree_libsearch.heading(key, text=text)
            self.tree_libsearch.column(key, width=self.px(width), anchor="w", stretch=stretch)
        vs = ttk.Scrollbar(wrap_s, orient="vertical", command=self.tree_libsearch.yview)
        self.tree_libsearch.configure(yscrollcommand=vs.set)
        self.tree_libsearch.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree_libsearch.bind("<Double-1>", self._install_selected_search)

        # --- 已安装 ---
        hrow = ttk.Frame(have)
        hrow.pack(fill="x")
        ttk.Label(hrow, style="Muted.TLabel",
                  text="装在 Arduino 用户目录下，编译时自动可用").pack(side="left")
        ttk.Button(hrow, text="刷新已安装库", command=self._start_lib_list).pack(side="right")
        ttk.Button(hrow, text="卸载选中", command=self._start_lib_uninstall).pack(side="right", padx=6)

        wrap_h = ttk.Frame(have)
        wrap_h.pack(fill="both", expand=True, pady=(6, 0))
        cols2 = ("name", "version", "author", "dir")
        self.tree_libs = ttk.Treeview(wrap_h, columns=cols2, show="headings", height=5,
                                      selectmode="extended")
        for key, (text, width, stretch) in {
            "name": ("库名称", 210, False), "version": ("版本", 80, False),
            "author": ("作者", 150, False), "dir": ("安装位置", 360, True),
        }.items():
            self.tree_libs.heading(key, text=text)
            self.tree_libs.column(key, width=self.px(width), anchor="w", stretch=stretch)
        vs2 = ttk.Scrollbar(wrap_h, orient="vertical", command=self.tree_libs.yview)
        self.tree_libs.configure(yscrollcommand=vs2.set)
        self.tree_libs.pack(side="left", fill="both", expand=True)
        vs2.pack(side="right", fill="y")
        self.after(400, self._set_lib_sash)

    def _set_lib_sash(self) -> None:
        """给“搜索库/已安装库”分栏一个合适的初始位置（布局完成后才有效）。"""
        try:
            self.update_idletasks()
            if self.lib_panes.winfo_height() > self.px(60):
                self.lib_panes.sashpos(0, self.px(170))
        except (tk.TclError, AttributeError):
            pass

    def _lib_source_hint(self) -> str:
        return ("按名称安装需要库索引（首次可点“更新库索引”，联网约 56MB）；"
                "ZIP / 本地文件夹安装无需联网，适合离线装自己下载的库。")

    def _update_lib_source(self) -> None:
        src = self.var_lib_source.get()
        if src == "name":
            self.btn_lib_browse.configure(state="disabled")
            self.entry_lib_input.configure(state="normal")
        elif src == "git":
            self.btn_lib_browse.configure(state="disabled")
            self.entry_lib_input.configure(state="normal")
        else:
            self.btn_lib_browse.configure(state="normal")
            self.entry_lib_input.configure(state="normal")

    def _pick_lib_source(self) -> None:
        src = self.var_lib_source.get()
        if src == "zip":
            path = filedialog.askopenfilename(
                title="选择库 ZIP 文件",
                filetypes=[("Arduino 库压缩包", "*.zip"), ("所有文件", "*.*")])
        elif src == "dir":
            path = filedialog.askdirectory(title="选择库文件夹（含 .h/.cpp 的库目录）")
        else:
            return
        if path:
            self.var_lib_input.set(os.path.normpath(path))

    def _open_library_dir(self) -> None:
        path = self.var_lib_dir.get().strip()
        if not path or not os.path.isdir(path):
            if esp_compile:
                tc = esp_compile.library_toolchain()
                path = esp_compile.library_dir(tc) if tc else ""
            if not path or not os.path.isdir(path):
                messagebox.showinfo(APP_NAME, "库目录还不存在，先安装一个库试试。")
                return
        try:
            if hasattr(os, "startfile"):
                os.startfile(path)  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"无法打开目录：{exc}")

    # ------------------------------------------------------- 库管理：任务
    def _lib_toolchain(self):
        chip = esp_compile.chip_from_text(self.conn_chip) if self.conn_chip else None
        return esp_compile.library_toolchain(chip=chip)

    def _require_lib(self) -> bool:
        if not self._require_compile():
            return False
        if self._lib_toolchain() is None:
            messagebox.showerror(APP_NAME, "未找到 arduino-cli，无法管理库。")
            return False
        return True

    def _start_lib_list(self) -> None:
        if not self._require_lib():
            return
        self._start_worker("刷新已安装库", self._task_lib_list, blocking=False)

    def _task_lib_list(self) -> None:
        tc = esp_compile.library_toolchain()
        libs = esp_compile.lib_list(tc, force=True)
        cores = "、".join(f"{k} {v}" for k, v in tc["cores"].items())
        self._emit("lib_list", dir=esp_compile.library_dir(tc), libs=libs,
                   env=f"{tc['label']}（{cores}）")

    def _start_lib_search(self) -> None:
        if not self._require_lib():
            return
        self._start_worker("搜索库", self._task_lib_search, blocking=False)

    def _task_lib_search(self) -> None:
        tc = esp_compile.library_toolchain()
        results, err = esp_compile.lib_search(tc, self._opt("lib_query", ""))
        if err:
            self._emit_log("warn", f"搜索库：{err}")
            return
        self._emit("lib_search", results=results)
        self._emit_log("ok", f"搜索到 {len(results)} 个库")

    def _start_lib_install(self) -> None:
        if not self._require_lib():
            return
        source = self.var_lib_source.get()
        text = self.var_lib_input.get().strip()
        if not text:
            messagebox.showwarning(APP_NAME, "请先填写要安装的内容：库名 / ZIP 路径 / Git 地址 / 文件夹。")
            return
        if source == "name":
            label = f"安装库 {text}"
        elif source == "zip":
            label = f"安装 ZIP {os.path.basename(text)}"
        elif source == "git":
            label = "安装 Git 库"
        else:
            label = f"安装库文件夹 {os.path.basename(text)}"
        self._start_worker(label, self._task_lib_install)

    def _task_lib_install(self) -> None:
        tc = esp_compile.library_toolchain()
        source = self._opt("lib_source", "name")
        text = (self._opt("lib_input") or "").strip()
        nodeps = bool(self._opt("lib_nodeps"))
        if source == "name":
            rc = esp_compile.lib_install(tc, on_line=self._log_line, names=[text], no_deps=nodeps)
        elif source == "zip":
            rc = esp_compile.lib_install(tc, on_line=self._log_line,
                                         zip_paths=[text], no_deps=nodeps)
        elif source == "git":
            rc = esp_compile.lib_install(tc, on_line=self._log_line,
                                         git_urls=[text], no_deps=nodeps)
        else:
            rc = esp_compile.lib_install_folder(tc, text, on_line=self._log_line, no_deps=nodeps)
        if rc != 0:
            raise RuntimeError(f"安装失败（arduino-cli 退出码 {rc}），详见运行日志")
        self._emit_log("ok", f"库安装完成：{text}")
        self._task_lib_list()

    def _start_lib_uninstall(self) -> None:
        if not self._require_lib():
            return
        names = self._selected_installed_libs()
        if not names:
            messagebox.showwarning(APP_NAME, "请先在“已安装库”表格里选中要卸载的库。")
            return
        if not messagebox.askokcancel(APP_NAME, "确定要卸载以下库吗？\n\n" + "\n".join(names)):
            return
        self._start_worker("卸载库", self._task_lib_uninstall)

    def _task_lib_uninstall(self) -> None:
        tc = esp_compile.library_toolchain()
        names = self._opt("lib_selected") or []
        rc = esp_compile.lib_uninstall(tc, names, on_line=self._log_line)
        if rc != 0:
            raise RuntimeError(f"卸载失败（arduino-cli 退出码 {rc}），详见运行日志")
        self._emit_log("ok", f"已卸载：{', '.join(names)}")
        self._task_lib_list()

    def _start_lib_update_index(self) -> None:
        if not self._require_lib():
            return
        self._start_worker("更新库索引", self._task_lib_update_index)

    def _task_lib_update_index(self) -> None:
        tc = esp_compile.library_toolchain()
        rc = esp_compile.lib_update_index(tc, on_line=self._log_line)
        if rc != 0:
            raise RuntimeError(f"更新库索引失败（退出码 {rc}），请检查网络连接")
        self._emit_log("ok", "库索引已更新，现在可以按名称搜索/安装库")

    def _start_lib_upgrade(self) -> None:
        if not self._require_lib():
            return
        if not messagebox.askokcancel(APP_NAME, "确定要升级全部已安装的库吗？（需要联网）"):
            return
        self._start_worker("升级全部库", self._task_lib_upgrade)

    def _task_lib_upgrade(self) -> None:
        tc = esp_compile.library_toolchain()
        rc = esp_compile.lib_upgrade(tc, on_line=self._log_line)
        if rc != 0:
            raise RuntimeError(f"升级失败（退出码 {rc}），详见运行日志")
        self._emit_log("ok", "库升级完成")
        self._task_lib_list()

    def _selected_installed_libs(self) -> list[str]:
        names = []
        for iid in self.tree_libs.selection():
            values = self.tree_libs.item(iid, "values")
            if values and values[0]:
                names.append(values[0])
        return names

    def _install_selected_search(self, _event=None) -> None:
        sel = self.tree_libsearch.selection()
        if not sel:
            return
        name = self.tree_libsearch.item(sel[0], "values")[0]
        self.var_lib_source.set("name")
        self._update_lib_source()
        self.var_lib_input.set(name)
        self._start_lib_install()

    def _apply_lib_list(self, ev: dict) -> None:
        libs = ev.get("libs") or []
        if ev.get("dir"):
            self.var_lib_dir.set(ev["dir"])
        for iid in self.tree_libs.get_children():
            self.tree_libs.delete(iid)
        for lib in libs:
            self.tree_libs.insert("", "end", values=(lib["name"], lib["version"],
                                                     lib["author"], lib["dir"]))
        self._log("info", f"已安装 {len(libs)} 个库（环境：{ev.get('env', '?')}，"
                          f"库目录：{ev.get('dir', '')}）")

    def _apply_lib_search(self, ev: dict) -> None:
        for iid in self.tree_libsearch.get_children():
            self.tree_libsearch.delete(iid)
        for lib in ev.get("results") or []:
            self.tree_libsearch.insert("", "end", values=(lib["name"], lib["latest"],
                                                          lib["author"], lib["sentence"]))

    def _apply_lib_hint(self, ev: dict) -> None:
        """编译因缺库失败时，把缺失的头文件对应的库名填进搜索框。"""
        header = ev.get("header") or ""
        guess = os.path.splitext(header)[0]
        self._select_tab("lib")
        self.var_lib_query.set(guess)
        self.var_lib_source.set("name")
        self._update_lib_source()
        self.var_lib_input.set(guess)
        self._log("warn", f"编译缺少头文件 {header}：已在“库管理”页填好搜索关键字 "
                          f"“{guess}”，点“搜索”确认后回车安装")
        self._start_lib_search()

    # ------------------------------------------------------------- 线程调度
    def _snapshot_options(self) -> dict:
        """在 GUI 线程中读取所有控件状态，供工作线程使用。

        Tk 变量只能在主线程访问，因此任务开始前必须先快照成普通字典。
        """
        try:
            baud = int(self.var_baud.get())
        except (tk.TclError, ValueError):
            baud = 115200
        return {
            "port": self._selected_port(),
            "before": self.var_before.get(),
            "stub": bool(self.var_stub.get()),
            "baud": baud,
            "flash_mode": self.var_flash_mode.get(),
            "flash_size": self.var_flash_size.get(),
            "flash_freq": self.var_flash_freq.get(),
            "erase": bool(self.var_erase.get()),
            "compress": bool(self.var_compress.get()),
            "verify": bool(self.var_verify.get()),
            "reset": bool(self.var_reset.get()),
            "reset_mode": self.var_reset_mode.get(),
            "read_addr": self.var_read_addr.get(),
            "read_size": self.var_read_size.get(),
            "read_out": self.var_read_out.get(),
            # 编译相关
            "compile_mode": self.var_compile_mode.get(),
            "c_src": self.var_c_src.get(),
            "c_fqbn": self.var_c_fqbn.get(),
            "c_out": self.var_c_out.get(),
            "c_merge": bool(self.var_c_merge.get()),
            "c_verbose": bool(self.var_c_verbose.get()),
            "i_src": self.var_i_src.get(),
            "i_target": self.var_i_target.get(),
            "i_merge": bool(self.var_i_merge.get()),
            "e_elf": self.var_e_elf.get(),
            "e_out": self.var_e_out.get(),
            "e_chip": self.var_e_chip.get(),
            "e_mode": self.var_e_mode.get(),
            "e_size": self.var_e_size.get(),
            "e_freq": self.var_e_freq.get(),
            "m_out": self.var_m_out.get(),
            "m_chip": self.var_m_chip.get(),
            "m_items": [(self.tree_merge.item(i, "values")[0],
                         self.tree_merge.item(i, "values")[1])
                        for i in self.tree_merge.get_children()],
            # 库管理相关
            "lib_source": self.var_lib_source.get(),
            "lib_input": self.var_lib_input.get(),
            "lib_query": self.var_lib_query.get(),
            "lib_nodeps": bool(self.var_lib_nodeps.get()),
            "lib_selected": self._selected_installed_libs(),
        }

    def _opt(self, key: str, default=None):
        """读取本工作线程自己的控件快照（见 _start_worker）。

        用线程本地变量而不是共享属性：只读任务（如刷新库列表）与
        编译/烧录任务可以并发，各自必须看到自己启动时的界面状态。
        """
        opts = getattr(self._worker_local, "opts", None)
        if opts is None:
            opts = getattr(self, "worker_opts", None) or {}
        return opts.get(key, default)

    def _emit(self, etype: str, **kw) -> None:
        kw["t"] = etype
        self.msg_q.put(kw)

    def _emit_log(self, level: str, text: str) -> None:
        self.msg_q.put({"t": "log", "level": level, "text": text})

    def _start_worker(self, label: str, target, blocking: bool = True) -> None:
        """启动后台任务。

        ``blocking=False`` 用于环境探测、开发板列表这类轻量只读任务：
        它们不会把界面置为“忙”，也不会互相抢占。
        """
        if ESPTOOL_IMPORT_ERROR is not None and label != "演示":
            messagebox.showerror(APP_NAME, f"esptool 不可用：\n{ESPTOOL_IMPORT_ERROR}")
            return
        # 每个工作线程一份控件快照（Tk 变量只能在主线程读）
        options = self._snapshot_options()
        self.worker_opts = options
        self._worker_local.opts = options
        if blocking:
            if self.busy:
                messagebox.showinfo(APP_NAME, "当前有任务正在运行，请稍候或点击“中止”。")
                return
            self.busy = True
            self.cancel_flag = False
            self._update_buttons()
            self._set_status(f"{label}…", "busy", "")
        self._emit_log("note", f"▶ {label} 开始")

        def runner():
            self._worker_local.opts = options
            try:
                target()
            except SystemExit as exc:
                self._emit_log("err", f"{label} 被终止: {exc}")
                if blocking:
                    self._emit("status", text=f"{label} 失败", kind="err", detail=str(exc)[:120])
            except Exception as exc:
                self._emit_log("err", f"{label} 出错: {exc}")
                self._emit_log("debug", traceback.format_exc())
                if blocking:
                    self._emit("status", text=f"{label} 出错", kind="err", detail=str(exc)[:120])
            finally:
                if blocking:
                    self.msg_q.put({"t": "busy", "value": False})
                self.msg_q.put({"t": "log", "level": "note", "text": f"■ {label} 结束"})

        threading.Thread(target=runner, daemon=True, name=label).start()

    def _cancel_task(self) -> None:
        if not self.busy:
            return
        self.cancel_flag = True
        self._log("warn", "已请求中止…（烧录中强行中止可能导致设备需要重新上电）")
        if self.esp is not None:
            self._close_esp(self.esp)
            self.esp = None
            self._log("warn", "已强制关闭串口以中止当前操作")

    # ------------------------------------------------------------- 日志工具
    def _save_log(self) -> None:
        path = filedialog.asksaveasfilename(title="保存日志", defaultextension=".txt",
                                            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.txt_log.get("1.0", "end"))
            self._log("ok", f"日志已保存到 {path}")
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"保存失败：{exc}")

    def _clear_log(self) -> None:
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")

    # ------------------------------------------------------------- 关闭程序
    def _on_close(self) -> None:
        if self.busy and not messagebox.askokcancel(APP_NAME, "仍有任务在运行，确定要退出吗？"):
            return
        try:
            self._disconnect_silent()
        except Exception:
            pass
        self.destroy()


# ---------------------------------------------------------------------------
def main() -> int:
    if ESPTOOL_IMPORT_ERROR is not None and not DEMO_MODE:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            APP_NAME,
            "无法导入 esptool，请先安装依赖：\n\n"
            "    python -m pip install -r requirements.txt\n\n"
            f"错误信息：{ESPTOOL_IMPORT_ERROR}")
        root.destroy()
        return 1
    try:  # Windows 高清屏适配
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    app = EspFlasherApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
