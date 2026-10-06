#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ESP 烧录器 —— 源码编译后端
===========================

把 C/C++ 源码变成可烧录的 ``.bin``：

1. **Arduino 工程**：调用 ``arduino-cli`` 编译 ``.ino`` / ``.cpp`` / ``.h``
   （若目录里没有 ``.ino``，会自动包装成一个临时 sketch）；
2. **ESP-IDF 工程**：调用 ``idf.py build``；
3. **ELF → BIN**：调用 esptool ``elf2image``，任何工具链产出的 ``.elf`` 都能转；
4. **BIN 合并**：调用 esptool ``merge_bin``，把 bootloader / 分区表 / 应用程序
   合成一个 ``factory.bin``。

本模块只做纯逻辑，不依赖 Tk，方便单独测试；所有外部命令都通过
``run_stream()`` 逐行回调，界面可以实时显示编译输出。
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
IS_WINDOWS = sys.platform == "win32"
TOOLCHAIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "toolchain")
BUNDLED_ARDUINO_CONFIG = os.path.join(TOOLCHAIN_DIR, "arduino-cli.yaml")


def arduino_config_args() -> list[str]:
    """Use the local C6 toolchain unless an explicit config is passed to build."""
    if os.path.isfile(BUNDLED_ARDUINO_CONFIG):
        return ["--config-file", BUNDLED_ARDUINO_CONFIG]
    return []


def arduino_short_tool_args() -> list[str]:
    """GCC 12 on Windows needs short tool paths for nested multilib headers."""
    if not IS_WINDOWS or not os.path.isfile(BUNDLED_ARDUINO_CONFIG):
        return []
    import ctypes
    result = []
    for name, alias, prop in [("esp-rv32", "rv", "tools.riscv32-esp-elf-gcc.path"),
                              ("esp32-arduino-libs", "libs", "tools.esp32-arduino-libs.path")]:
        paths = glob.glob(os.path.join(TOOLCHAIN_DIR, "arduino-data", "packages",
                                       "esp32", "tools", name, "*"))
        path = first_existing(sorted(paths, reverse=True))
        path = first_existing([os.path.join(TOOLCHAIN_DIR, alias), path])
        if path:
            buf = ctypes.create_unicode_buffer(32768)
            if ctypes.windll.kernel32.GetShortPathNameW(path, buf, len(buf)):
                result.extend(["--build-property", prop + "=" + buf.value])
    return result

#: arduino-cli 常见位置（Arduino IDE 2.x 自带一份）
ARDUINO_CLI_CANDIDATES = [
    os.path.join(TOOLCHAIN_DIR, "bin", "arduino-cli.exe"),
    r"C:\Program Files\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe",
    r"C:\Program Files (x86)\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe",
    os.path.expandvars(
        r"%LOCALAPPDATA%\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe"
    ),
    os.path.expandvars(
        r"%LOCALAPPDATA%\Programs\arduino-ide\resources\app\lib\backend\resources\arduino-cli.exe"
    ),
    "/usr/local/bin/arduino-cli",
    "/usr/bin/arduino-cli",
    "/opt/homebrew/bin/arduino-cli",
]

#: Arduino 数据目录（core 与工具链安装在这里）
ARDUINO_DATA_DIRS = [
    os.path.join(TOOLCHAIN_DIR, "arduino-data"),
    os.path.expandvars(r"%LOCALAPPDATA%\Arduino15"),
    os.path.expandvars(r"%APPDATA%\arduino15"),
    os.path.expanduser("~/.arduino15"),
    os.path.expanduser("~/Library/Arduino15"),
]

#: 默认 FQBN（常用开发板），用户也可以点“刷新开发板”从 arduino-cli 取全量列表
DEFAULT_FQBNS = [
    "esp32:esp32:esp32",
    "esp32:esp32:esp32s3",
    "esp32:esp32:esp32c3",
    "esp32:esp32:esp32s2",
    "esp32:esp32:esp32c6",
    "esp8266:esp8266:generic",
]

#: 芯片 → bootloader 烧录偏移（ESP32 系列在 0x1000，其余在 0x0）
BOOTLOADER_OFFSETS = {
    "esp32": 0x1000,
    "esp32s2": 0x1000,
    "esp32s3": 0x0,
    "esp32c3": 0x0,
    "esp32c2": 0x0,
    "esp32c6": 0x0,
    "esp32h2": 0x0,
    "esp32p4": 0x2000,
    "esp8266": 0x0,
}

PARTITION_OFFSET = 0x8000
BOOT_APP0_OFFSET = 0xE000
APP_OFFSET = 0x10000

SOURCE_SUFFIXES = (".ino", ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh", ".S", ".s")

#: 支持的芯片（用于 elf2image / merge_bin 的 chip 参数）
CHIP_CHOICES = [
    "esp32", "esp32s2", "esp32s3", "esp32c3", "esp32c2", "esp32c6", "esp32h2",
    "esp32p4", "esp8266",
]

_CHIP_PATTERNS = [
    ("esp32s3", "esp32s3"), ("esp32s2", "esp32s2"), ("esp32c3", "esp32c3"),
    ("esp32c2", "esp32c2"), ("esp32c6", "esp32c6"), ("esp32h2", "esp32h2"),
    ("esp32p4", "esp32p4"), ("esp8266", "esp8266"), ("esp32", "esp32"),
]


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------
def first_existing(paths) -> str | None:
    for path in paths:
        if path and os.path.exists(path):
            return path
    return None


def which(name: str) -> str | None:
    return shutil.which(name)


def chip_from_text(text: str) -> str | None:
    """从 FQBN / 文件名 / 芯片名里猜出芯片型号。

    会先去掉 ``-`` ``_`` 和空格，所以 ``ESP32-C6``、``esp32_c6``、``esp32c6``
    都能识别成 ``esp32c6``。这个归一化很关键：esptool 报出的芯片名是
    ``ESP32-C6`` 这种带连字符的写法，不归一化会被误判成 ``esp32``。
    """
    low = re.sub(r"[\s\-_]+", "", (text or "").lower())
    for pattern, chip in _CHIP_PATTERNS:
        if pattern in low:
            return chip
    return None


def chip_from_fqbn(fqbn: str) -> str:
    return chip_from_text(fqbn) or "esp32"


def chip_display(chip: str) -> str:
    """esp32c6 -> ESP32-C6（仅用于显示）。"""
    low = (chip or "").lower()
    if low.startswith("esp32") and len(low) > 5:
        return "ESP32-" + low[5:].upper()
    return low.upper()


def check_fqbn(cli: str, fqbn: str, config_args: list[str] | None = None) -> tuple[bool, str]:
    """检查 FQBN 是否被本机已安装的核心支持，返回 (是否可用, 原因/详情)。"""
    if not cli:
        return False, "未找到 arduino-cli"
    if not fqbn or fqbn.count(":") < 2:
        return False, "FQBN 格式应为 包:架构:板卡，例如 esp32:esp32:esp32"
    if config_args is None:
        config_args = arduino_config_args()
    try:
        proc = subprocess.run([cli, *config_args, "board", "details", "--fqbn", fqbn],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120)
    except Exception as exc:
        return False, f"检查失败：{exc}"
    if proc.returncode == 0:
        return True, ""
    lines = [ln.strip() for ln in ((proc.stderr or "") + (proc.stdout or "")).splitlines()
             if ln.strip()]
    return False, (lines[-1] if lines else "未知错误")


def bootloader_offset(chip: str) -> int:
    return BOOTLOADER_OFFSETS.get(chip, 0x1000)


def run_stream(cmd, cwd=None, on_line=None, env=None, use_shell=False) -> int:
    """执行命令并把输出逐行回调给 ``on_line``，返回退出码。"""
    if on_line:
        shown = cmd if isinstance(cmd, str) else subprocess.list2cmdline(list(cmd))
        on_line(f"$ {shown}")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            shell=use_shell,
        )
    except FileNotFoundError:
        if on_line:
            on_line(f"[错误] 找不到可执行文件：{cmd[0] if isinstance(cmd, (list, tuple)) else cmd}")
        return 127
    except OSError as exc:
        if on_line:
            on_line(f"[错误] 无法执行命令：{exc}")
        return 126

    assert proc.stdout is not None
    for line in proc.stdout:
        if on_line:
            on_line(line.rstrip("\r\n"))
    return proc.wait()


def parse_addr(text: str, default: int = 0) -> int:
    text = (text or "").strip()
    if not text:
        return default
    return int(text, 0)


# ---------------------------------------------------------------------------
# Arduino 工具链探测
# ---------------------------------------------------------------------------
def find_arduino_cli() -> str | None:
    env_cli = os.environ.get("ARDUINO_CLI")
    if env_cli and os.path.exists(env_cli):
        return env_cli
    return first_existing(ARDUINO_CLI_CANDIDATES) or which("arduino-cli")


def arduino_data_dir() -> str | None:
    return first_existing(ARDUINO_DATA_DIRS)


def arduino_cores(cli: str, config_args: list[str] | None = None) -> list[dict]:
    """返回已安装的核心列表：[{id, installed, latest, name}]。

    ``config_args`` 为 ``None`` 时沿用全局配置（内置工具链），传 ``[]``
    表示使用 arduino-cli 的默认数据目录（系统 Arduino IDE）。
    """
    if not cli:
        return []
    if config_args is None:
        config_args = arduino_config_args()
    try:
        proc = subprocess.run([cli, *config_args, "core", "list", "--format", "json"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=90)
        if proc.returncode == 0 and proc.stdout.strip():
            data = json.loads(proc.stdout)
            cores = []
            for item in data if isinstance(data, list) else data.get("platforms", []):
                cores.append({
                    "id": item.get("id") or item.get("ID") or "",
                    "installed": item.get("installed_version")
                    or item.get("installed") or "",
                    "latest": item.get("latest_version") or item.get("latest") or "",
                    "name": item.get("name") or "",
                })
            return [c for c in cores if c["id"]]
    except Exception:
        pass

    # 退回到文本解析
    try:
        proc = subprocess.run([cli, *config_args, "core", "list"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=90)
    except Exception:
        return []
    cores = []
    for line in proc.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and ":" in parts[0]:
            cores.append({"id": parts[0], "installed": parts[1],
                          "latest": parts[2] if len(parts) > 2 else "",
                          "name": " ".join(parts[3:])})
    return cores


def arduino_boards(cli: str, search: str = "esp",
                   config_args: list[str] | None = None) -> list[tuple[str, str]]:
    """返回 (板子名称, FQBN) 列表（带缓存：board listall 比较慢，界面上会卡）。"""
    if not cli:
        return []
    if config_args is None:
        config_args = arduino_config_args()
    key = (os.path.normcase(cli), search, tuple(config_args))
    cached = _BOARD_CACHE.get(key)
    now = time.time()
    if cached and now - cached[0] < 300:
        return cached[1]
    for args in (["board", "listall", search, "--format", "json"],
                 ["board", "listall", "--format", "json"]):
        try:
            proc = subprocess.run([cli, *config_args, *args], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=180)
        except Exception:
            continue
        if proc.returncode != 0 or not proc.stdout.strip():
            continue
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            continue
        boards = []
        for item in data.get("boards", []):
            fqbn = item.get("fqbn") or item.get("FQBN") or ""
            if not fqbn:
                continue
            if search and not fqbn.lower().startswith(search.lower()[:6]):
                continue
            boards.append((item.get("name", fqbn), fqbn))
        if boards:
            result = sorted(boards)
            _BOARD_CACHE[key] = (now, result)
            return result
    return []


def core_install_dir(data_dir: str | None, core_id: str) -> str | None:
    """根据 core id（如 esp32:esp32）找到安装目录。"""
    if not data_dir or not core_id:
        return None
    pkg, _, arch = core_id.partition(":")
    base = os.path.join(data_dir, "packages", pkg, "hardware", arch)
    if not os.path.isdir(base):
        return None
    versions = sorted(
        (os.path.join(base, name) for name in os.listdir(base)
         if os.path.isdir(os.path.join(base, name))),
        key=os.path.getmtime,
        reverse=True,
    )
    return versions[0] if versions else None


def read_board_bootloader_addr(core_dir: str | None, fqbn: str) -> int | None:
    """从 boards.txt 里读取该板子的 bootloader 烧录偏移。"""
    if not core_dir:
        return None
    parts = fqbn.split(":")
    if len(parts) < 3:
        return None
    board_id = parts[2]
    boards_txt = os.path.join(core_dir, "boards.txt")
    if not os.path.isfile(boards_txt):
        return None
    key = f"{board_id}.build.bootloader_addr="
    try:
        with open(boards_txt, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(key):
                    return int(line.split("=", 1)[1].strip(), 0)
    except OSError:
        return None
    return None


def core_boot_app0(core_dir: str | None) -> str | None:
    if not core_dir:
        return None
    path = os.path.join(core_dir, "tools", "partitions", "boot_app0.bin")
    return path if os.path.isfile(path) else None


# ---------------------------------------------------------------------------
# 源码 → sketch 包装
# ---------------------------------------------------------------------------
def sanitize_sketch_name(name: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_]", "_", name or "").strip("_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = "sketch_" + cleaned
    return cleaned[:60] or "sketch"


def find_sources(src_dir: str) -> list[str]:
    out = []
    for root, _dirs, files in os.walk(src_dir):
        for fname in files:
            if fname.lower().endswith(SOURCE_SUFFIXES):
                out.append(os.path.join(root, fname))
    return out


def work_root() -> str:
    root = os.path.join(tempfile.gettempdir(), "esp_flasher_gui")
    os.makedirs(root, exist_ok=True)
    return root


def wrap_sources_as_sketch(src_dir: str, root: str | None = None,
                           on_line=None) -> tuple[str, bool]:
    """把一个只放了 .cpp/.h 的目录包装成 Arduino sketch 目录。

    返回 ``(sketch 目录, 是否新建了包装目录)``。
    若目录内已有 ``.ino``，直接返回原目录。
    """
    src_dir = os.path.abspath(src_dir)
    sources = find_sources(src_dir)
    if any(p.lower().endswith(".ino") for p in sources):
        return src_dir, False

    name = sanitize_sketch_name(os.path.basename(os.path.normpath(src_dir)))
    dest = os.path.join(root or work_root(), name)
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest, exist_ok=True)
    copied = 0
    for path in sources:
        rel = os.path.relpath(path, src_dir)
        target = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(path, target)
        copied += 1
    sketch_file = os.path.join(dest, name + ".ino")
    with open(sketch_file, "w", encoding="utf-8") as fh:
        fh.write(
            "// 由 ESP 烧录器自动生成的空 sketch：\n"
            "// 真正的 setup()/loop() 由同目录下的 .cpp 文件提供。\n"
        )
    if on_line:
        on_line(f"[信息] 目录内没有 .ino，已把 {copied} 个源文件包装为临时 sketch：{dest}")
    return dest, True


# ---------------------------------------------------------------------------
# Arduino 编译
# ---------------------------------------------------------------------------
def build_arduino(cli: str, fqbn: str, sketch_dir: str, out_dir: str,
                  on_line=None, extra_args=None, verbose: bool = False,
                  config_args: list[str] | None = None,
                  short_tool_args: list[str] | None = None) -> int:
    """调用 arduino-cli 编译。

    ``config_args`` / ``short_tool_args`` 由 :func:`resolve_toolchain` 给出：
    内置 C6 工具链需要 ``--config-file`` 和短路径 ``--build-property``，
    系统 Arduino IDE 则两者都不需要。都不传时保持原有行为。
    """
    os.makedirs(out_dir, exist_ok=True)
    cmd = [cli, "compile", "--fqbn", fqbn, "--output-dir", out_dir]
    if config_args is None:
        config_args = arduino_config_args()
    if short_tool_args is None:
        short_tool_args = arduino_short_tool_args()
    if not any(arg == "--config-file" or arg.startswith("--config-file=")
               for arg in (extra_args or [])):
        cmd.extend(config_args)
        cmd.extend(short_tool_args)
    if verbose:
        cmd.append("--verbose")
    cmd.extend(extra_args or [])
    cmd.append(sketch_dir)
    return run_stream(cmd, on_line=on_line)


def collect_arduino_items(out_dir: str, fqbn: str, core_dir: str | None,
                          on_line=None) -> list[tuple[int, str]]:
    """根据编译产物推导出 (地址, 文件) 合并清单。"""
    if not os.path.isdir(out_dir):
        return []
    files = [os.path.join(out_dir, f) for f in os.listdir(out_dir)
             if f.lower().endswith(".bin")]
    if not files:
        return []

    chip = chip_from_fqbn(fqbn)
    bootloader = next((f for f in files if "bootloader" in os.path.basename(f).lower()), None)
    partitions = next((f for f in files if "partitions" in os.path.basename(f).lower()), None)
    app = next((f for f in files
                if "bootloader" not in os.path.basename(f).lower()
                and "partitions" not in os.path.basename(f).lower()), None)

    items: list[tuple[int, str]] = []
    if chip == "esp8266":
        if app:
            items.append((0x0, app))
        if partitions:
            items.append((0x100000, partitions))
        return items

    if bootloader:
        offset = read_board_bootloader_addr(core_dir, fqbn)
        if offset is None:
            offset = bootloader_offset(chip)
        items.append((offset, bootloader))
    if partitions:
        items.append((PARTITION_OFFSET, partitions))
        boot_app0 = core_boot_app0(core_dir)
        if boot_app0:
            items.append((BOOT_APP0_OFFSET, boot_app0))
    if app:
        items.append((APP_OFFSET, app))
    if on_line:
        for addr, path in items:
            on_line(f"[信息] 合并清单 {addr:#08x}  {path}")
    return items


# ---------------------------------------------------------------------------
# ESP-IDF
# ---------------------------------------------------------------------------
def find_idf() -> dict | None:
    """探测 ESP-IDF，返回 {root, export, idf_py} 或 None。"""
    roots = []
    if os.environ.get("IDF_PATH"):
        roots.append(os.environ["IDF_PATH"])
    roots += sorted(glob.glob(r"C:\Espressif\frameworks\esp-idf-*"), reverse=True)
    roots += sorted(glob.glob(os.path.expanduser("~/esp/esp-idf*")), reverse=True)
    roots += sorted(glob.glob(os.path.expanduser("~/.espressif/frameworks/*")), reverse=True)

    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        export = None
        for name in ("export.bat", "export.ps1", "export.sh"):
            cand = os.path.join(root, name)
            if os.path.isfile(cand):
                export = cand
                break
        if export:
            idf_py = os.path.join(root, "tools", "idf.py")
            return {"root": root, "export": export,
                    "idf_py": idf_py if os.path.isfile(idf_py) else (which("idf.py") or "idf.py")}

    idf_py = which("idf.py")
    if idf_py:
        return {"root": os.environ.get("IDF_PATH", ""), "export": None, "idf_py": idf_py}
    return None


def build_idf(proj_dir: str, build_dir: str, idf: dict, target: str = "",
              on_line=None) -> int:
    """在 IDF 环境下执行 idf.py build。"""
    os.makedirs(build_dir, exist_ok=True)
    extra = f" -DIDF_TARGET={target}" if target else ""
    idf_py = idf.get("idf_py") or "idf.py"
    export = idf.get("export")

    if IS_WINDOWS and export and export.lower().endswith(".bat"):
        # 通过临时 bat 调用 export.bat，避免 cmd 嵌套引号问题
        bat = os.path.join(work_root(), "idf_build.bat")
        with open(bat, "w", encoding="gbk", errors="replace") as fh:
            fh.write("@echo off\r\n")
            fh.write(f'call "{export}"\r\n')
            fh.write("if errorlevel 1 exit /b 1\r\n")
            fh.write(f'cd /d "{os.path.abspath(proj_dir)}"\r\n')
            fh.write(f'idf.py -B "{os.path.abspath(build_dir)}"{extra} build\r\n')
            fh.write("exit /b %errorlevel%\r\n")
        return run_stream(["cmd", "/c", bat], on_line=on_line)

    if export and export.lower().endswith(".sh"):
        script = (f'source "{export}" && cd "{os.path.abspath(proj_dir)}" && '
                  f'idf.py -B "{os.path.abspath(build_dir)}"{extra} build')
        return run_stream(["bash", "-c", script], on_line=on_line)

    cmd = [idf_py, "-B", os.path.abspath(build_dir)]
    if target:
        cmd.append(f"-DIDF_TARGET={target}")
    cmd.append("build")
    return run_stream(cmd, cwd=os.path.abspath(proj_dir), on_line=on_line)


def collect_idf_items(build_dir: str, project_name: str, chip: str,
                      on_line=None) -> list[tuple[int, str]]:
    items: list[tuple[int, str]] = []
    bootloader = os.path.join(build_dir, "bootloader", "bootloader.bin")
    partitions = os.path.join(build_dir, "partition_table", "partition-table.bin")
    ota_data = os.path.join(build_dir, "ota_data_initial.bin")
    app = os.path.join(build_dir, f"{project_name}.bin")

    if os.path.isfile(bootloader):
        items.append((bootloader_offset(chip), bootloader))
    if os.path.isfile(partitions):
        items.append((PARTITION_OFFSET, partitions))
    if os.path.isfile(ota_data):
        items.append((BOOT_APP0_OFFSET, ota_data))
    if os.path.isfile(app):
        items.append((APP_OFFSET, app))
    else:  # 工程名与 bin 名不一致时，取 build 根目录下最大的 bin
        cands = [os.path.join(build_dir, f) for f in os.listdir(build_dir)
                 if f.endswith(".bin")] if os.path.isdir(build_dir) else []
        cands = [c for c in cands if os.path.basename(c) not in
                 ("bootloader.bin", "partition-table.bin", "ota_data_initial.bin")]
        if cands:
            items.append((APP_OFFSET, max(cands, key=os.path.getsize)))
    if on_line:
        for addr, path in items:
            on_line(f"[信息] 合并清单 {addr:#08x}  {path}")
    return items


# ---------------------------------------------------------------------------
# esptool 侧：ELF → BIN / 合并 BIN
# ---------------------------------------------------------------------------
def elf_to_bin(elf_path: str, chip: str, out_path: str, flash_mode: str = "keep",
               flash_size: str = "keep", flash_freq: str = "keep") -> None:
    from esptool.cmds import elf2image

    elf2image(
        elf_path,
        chip,
        output=out_path,
        flash_freq=None if flash_freq in ("", "keep") else flash_freq,
        flash_mode=flash_mode if flash_mode != "keep" else "qio",
        flash_size=flash_size if flash_size != "keep" else "1MB",
    )


def merge_bins(items: list[tuple[int, str]], chip: str, out_path: str,
               flash_mode: str = "keep", flash_size: str = "keep",
               flash_freq: str = "keep", out_format: str = "raw",
               on_line=None) -> None:
    from esptool.cmds import merge_bin

    if not items:
        raise ValueError("没有需要合并的镜像文件")
    for addr, path in items:
        if not os.path.isfile(path):
            raise FileNotFoundError(f"文件不存在：{path}")
        if on_line:
            on_line(f"[信息] 合并 {addr:#08x}  {path}（{os.path.getsize(path)} 字节）")
    merge_bin(
        [(addr, path) for addr, path in items],
        chip,
        output=out_path,
        flash_freq=flash_freq or "keep",
        flash_mode=flash_mode or "keep",
        flash_size=flash_size or "keep",
        format=out_format,
    )
    if on_line and os.path.isfile(out_path):
        on_line(f"[完成] 已生成 {out_path}（{os.path.getsize(out_path)} 字节）")


# ---------------------------------------------------------------------------
# 多工具链探测与“按已连接设备自动选择”
# ---------------------------------------------------------------------------
#: 芯片 → 可能提供其编译器的工具目录名（覆盖 Arduino 核心 2.x / 3.x 两代命名）
CHIP_TOOL_DIRS = {
    "esp32": ("xtensa-esp32-elf-gcc", "esp-xtensa-esp-elf-gcc", "esp-xtensa-elf-gcc"),
    "esp32s2": ("xtensa-esp32s2-elf-gcc", "esp-xtensa-esp-elf-gcc", "esp-xtensa-elf-gcc"),
    "esp32s3": ("xtensa-esp32s3-elf-gcc", "esp-xtensa-esp-elf-gcc", "esp-xtensa-elf-gcc"),
    "esp32c2": ("riscv32-esp-elf-gcc", "esp-rv32"),
    "esp32c3": ("riscv32-esp-elf-gcc", "esp-rv32"),
    "esp32c6": ("riscv32-esp-elf-gcc", "esp-rv32"),
    "esp32h2": ("riscv32-esp-elf-gcc", "esp-rv32"),
    "esp32p4": ("riscv32-esp-elf-gcc", "esp-rv32"),
    "esp8266": ("xtensa-lx106-elf-gcc",),
}

#: 芯片 → 所属 Arduino 包名
CHIP_PACKAGE = {"esp8266": "esp8266"}

_TOOLCHAIN_CACHE: dict = {"time": 0.0, "list": []}
_BOARD_CACHE: dict = {}


def _toolchain_candidate(kind: str, label: str, cli: str | None,
                         data_dir: str | None, config_file: str | None) -> dict | None:
    if not cli or not os.path.exists(cli):
        return None
    if kind == "bundled" and not os.path.isfile(BUNDLED_ARDUINO_CONFIG):
        return None
    if kind == "system" and config_file == BUNDLED_ARDUINO_CONFIG:
        return None
    return {
        "kind": kind,
        "label": label,
        "cli": cli,
        "data_dir": data_dir,
        "config_file": config_file,
        "config_args": ["--config-file", config_file] if config_file else [],
        "short_tool_args": arduino_short_tool_args() if kind == "bundled" else [],
        "cores": {},
        "chips": set(),
    }


def _system_arduino_cli() -> str | None:
    env_cli = os.environ.get("ARDUINO_CLI")
    if env_cli and os.path.exists(env_cli):
        return env_cli
    candidates = [p for p in ARDUINO_CLI_CANDIDATES
                  if os.path.normcase(p) != os.path.normcase(os.path.join(TOOLCHAIN_DIR, "bin", "arduino-cli.exe"))]
    return first_existing(candidates) or which("arduino-cli")


def _system_data_dir() -> str | None:
    bundled = os.path.normcase(os.path.join(TOOLCHAIN_DIR, "arduino-data"))
    candidates = [p for p in ARDUINO_DATA_DIRS if os.path.normcase(p) != bundled]
    return first_existing(candidates)


def installed_tool_dirs(data_dir: str | None, package: str = "esp32") -> set[str]:
    """列出某个数据目录下已安装的工具目录名（如 riscv32-esp-elf-gcc）。"""
    base = os.path.join(data_dir or "", "packages", package, "tools")
    if not os.path.isdir(base):
        return set()
    return {name for name in os.listdir(base) if os.path.isdir(os.path.join(base, name))}


def core_chips(core_dir: str | None) -> set[str]:
    """从 boards.txt 里读出该核心支持的全部芯片（build.mcu 取值）。"""
    if not core_dir:
        return set()
    boards_txt = os.path.join(core_dir, "boards.txt")
    if not os.path.isfile(boards_txt):
        return set()
    chips = set()
    try:
        with open(boards_txt, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if ".build.mcu=" in line:
                    value = line.split("=", 1)[1].strip().lower()
                    if value:
                        chips.add(value)
    except OSError:
        return set()
    return chips


def toolchain_chips(tc: dict) -> set[str]:
    """该工具链**实际可编译**的芯片：核心支持 ∩ 编译器已安装。"""
    chips: set[str] = set()
    for core_id in tc.get("cores", {}):
        core_dir = core_install_dir(tc.get("data_dir"), core_id)
        package = core_id.split(":")[0] or "esp32"
        tools = installed_tool_dirs(tc.get("data_dir"), package)
        for chip in core_chips(core_dir):
            required = CHIP_TOOL_DIRS.get(chip)
            if required and (tools & set(required)):
                chips.add(chip)
    return chips


def detect_toolchains(force: bool = False, ttl: float = 300.0) -> list[dict]:
    """探测本机所有可用的 Arduino 工具链（内置 + 系统），带缓存。"""
    now = time.time()
    if not force and _TOOLCHAIN_CACHE["list"] and now - _TOOLCHAIN_CACHE["time"] < ttl:
        return _TOOLCHAIN_CACHE["list"]

    candidates = [
        _toolchain_candidate("bundled", "内置工具链", find_arduino_cli(),
                             os.path.join(TOOLCHAIN_DIR, "arduino-data"),
                             BUNDLED_ARDUINO_CONFIG if os.path.isfile(BUNDLED_ARDUINO_CONFIG) else None),
        _toolchain_candidate("system", "系统 Arduino IDE", _system_arduino_cli(),
                             _system_data_dir(), None),
    ]
    result: list[dict] = []
    seen: set[str] = set()
    for tc in candidates:
        if not tc:
            continue
        key = os.path.normcase(tc["cli"]) + "|" + os.path.normcase(tc["data_dir"] or "")
        if key in seen:
            continue
        seen.add(key)
        cores = arduino_cores(tc["cli"], config_args=tc["config_args"])
        tc["cores"] = {c["id"]: c["installed"] for c in cores if c["id"]}
        tc["core_names"] = {c["id"]: c["name"] for c in cores if c["id"]}
        tc["chips"] = toolchain_chips(tc)
        result.append(tc)

    _TOOLCHAIN_CACHE["list"] = result
    _TOOLCHAIN_CACHE["time"] = now
    return result


def resolve_toolchain(chip: str | None = None, fqbn: str | None = None,
                      force: bool = False) -> dict | None:
    """按目标芯片/开发板挑选合适的工具链。

    规则：先看哪个工具链装了该 FQBN 所属的核心，再看它是否真的支持这块芯片，
    内置工具链优先（它带着 C6 用的 RISC-V 工具链）。
    """
    toolchains = detect_toolchains(force=force)
    if not toolchains:
        return None
    want_chip = chip or (chip_from_fqbn(fqbn) if fqbn else None)
    pkg_arch = ":".join(fqbn.split(":")[:2]) if fqbn else None

    if pkg_arch:
        with_core = [t for t in toolchains if pkg_arch in t["cores"]]
        for tc in with_core:
            if not want_chip or want_chip in tc["chips"]:
                return tc
        if with_core:
            return with_core[0]

    if want_chip:
        for tc in toolchains:               # 内置优先
            if want_chip in tc["chips"]:
                return tc
    return toolchains[0]


def boards_for_chip(chip: str, toolchain: dict | None = None,
                    search: str | None = None) -> list[str]:
    """列出某个工具链里支持该芯片的 FQBN（常用板卡排前面）。"""
    tc = toolchain or resolve_toolchain(chip=chip)
    if not tc:
        return []
    prefix = "esp8266" if chip == "esp8266" else "esp32"
    boards = arduino_boards(tc["cli"], search or prefix, config_args=tc["config_args"])
    fqbns = []
    for _name, fqbn in boards:
        if chip_from_text(fqbn) == chip:
            fqbns.append(fqbn)
    # 规范化板卡（esp32:esp32:esp32c6）排最前，其余按名称排序
    canonical = f"{prefix}:{prefix}:{chip}"
    fqbns = sorted(set(fqbns))
    if canonical in fqbns:
        fqbns.remove(canonical)
        fqbns.insert(0, canonical)
    return fqbns


def describe_toolchains(toolchains: list[dict] | None = None) -> list[str]:
    """把工具链能力汇总成几行文字，供界面显示。"""
    lines = []
    for tc in toolchains if toolchains is not None else detect_toolchains():
        cores = "、".join(f"{cid} {ver}".strip() for cid, ver in tc["cores"].items()) or "无核心"
        chips = ", ".join(chip_display(c) for c in sorted(tc["chips"])) or "无可用编译器"
        lines.append(f"{tc['label']}（{cores}）→ 可编译：{chips}")
    if not lines:
        lines.append("未找到任何 Arduino 工具链")
    return lines


# ---------------------------------------------------------------------------
# 库管理（arduino-cli lib ...）
# ---------------------------------------------------------------------------
_USER_DIR_CACHE: dict = {}
_LIB_LIST_CACHE: dict = {}


def sketchbook_dir(tc: dict) -> str:
    """Arduino 用户目录（libraries 就放在它下面）。"""
    key = os.path.normcase(tc["cli"]) + "|" + "|".join(tc.get("config_args") or [])
    if key in _USER_DIR_CACHE:
        return _USER_DIR_CACHE[key]
    path = None
    try:
        proc = subprocess.run([tc["cli"], *tc.get("config_args", []),
                               "config", "get", "directories.user"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)
        if proc.returncode == 0:
            path = proc.stdout.strip().strip('"').strip("'") or None
    except Exception:
        path = None
    if not path:
        path = os.path.join(os.path.expanduser("~"), "Documents", "Arduino")
    path = os.path.normpath(path)
    _USER_DIR_CACHE[key] = path
    return path


def library_dir(tc: dict) -> str:
    """已安装库的目录（<Arduino 用户目录>/libraries）。"""
    return os.path.join(sketchbook_dir(tc), "libraries")


def library_toolchain(chip: str | None = None) -> dict | None:
    """做库管理用哪个 arduino-cli（两个环境的库目录是同一个）。"""
    return resolve_toolchain(chip=chip)


def lib_list(tc: dict, force: bool = False) -> list[dict]:
    """已安装库列表。"""
    key = os.path.normcase(tc["cli"]) + "|" + "|".join(tc.get("config_args") or [])
    cached = _LIB_LIST_CACHE.get(key)
    now = time.time()
    if not force and cached and now - cached[0] < 20:
        return cached[1]
    try:
        proc = subprocess.run([tc["cli"], *tc.get("config_args", []),
                               "lib", "list", "--format", "json"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120)
        data = json.loads(proc.stdout or "{}")
    except Exception:
        return []
    out = []
    for item in data.get("installed_libraries") or []:
        lib = item.get("library") or item
        out.append({
            "name": lib.get("name", ""),
            "version": lib.get("version", ""),
            "author": lib.get("author", "") or lib.get("maintainer", ""),
            "dir": lib.get("install_dir", ""),
            "includes": lib.get("provides_includes") or [],
            "sentence": lib.get("sentence", ""),
            "location": lib.get("location", ""),
        })
    result = sorted(out, key=lambda x: (x["name"] or "").lower())
    _LIB_LIST_CACHE[key] = (now, result)
    return result


def lib_search(tc: dict, query: str, limit: int = 40) -> tuple[list[dict], str]:
    """按关键字搜索库，返回 (结果, 错误信息)。"""
    if not (query or "").strip():
        return [], "请输入搜索关键字"
    try:
        proc = subprocess.run([tc["cli"], *tc.get("config_args", []),
                               "lib", "search", query.strip(), "--format", "json"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=180)
    except Exception as exc:
        return [], f"搜索失败：{exc}"
    if not proc.stdout.strip():
        err = (proc.stderr or "").strip().splitlines()
        return [], (err[-1] if err else "没有任何结果（可先点“更新库索引”）")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return [], f"解析搜索结果失败：{exc}"
    out = []
    for item in (data.get("libraries") or [])[:limit]:
        lib = item.get("library") or item
        latest = item.get("latest") or item.get("release") or {}
        out.append({
            "name": lib.get("name") or item.get("name") or "",
            "latest": latest.get("version") or item.get("version") or "",
            "versions": item.get("available_versions") or [],
            "author": latest.get("author") or lib.get("author") or "",
            "sentence": (latest.get("sentence") or lib.get("sentence") or "")[:120],
        })
    return out, ""


def _lib_cmd(tc: dict, *args) -> list[str]:
    return [tc["cli"], *tc.get("config_args", []), "lib", *args]


def _yaml_lines(data, indent: int = 0) -> list[str]:
    """把简单的嵌套 dict/list 转成 YAML（只为写 arduino-cli 配置，够用即可）。"""
    lines = []
    pad = " " * indent
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append(f"{pad}{key}:")
            lines.extend(_yaml_lines(value, indent + 2))
        elif isinstance(value, (list, tuple)):
            lines.append(f"{pad}{key}:")
            for item in value:
                lines.append(f"{pad}  - {item}")
        elif isinstance(value, bool):
            lines.append(f"{pad}{key}: {'true' if value else 'false'}")
        else:
            lines.append(f"{pad}{key}: {value}")
    return lines


def unsafe_install_config_args(tc: dict) -> list[str]:
    """返回用于 zip/git 安装的 ``--config-file`` 参数。

    arduino-cli 1.5 默认禁用 ``--zip-path`` / ``--git-url``，需要在配置里打开
    ``library.enable_unsafe_install``。这里把**当前生效配置**导出成临时配置并打开该开关，
    不会改动用户自己的配置文件。
    """
    cached = tc.get("_unsafe_config")
    if cached and os.path.isfile(cached):
        return ["--config-file", cached]
    cfg: dict = {}
    try:
        proc = subprocess.run([tc["cli"], *tc.get("config_args", []),
                               "config", "dump", "--format", "json"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)
        if proc.returncode == 0 and proc.stdout.strip():
            data = json.loads(proc.stdout)
            cfg = data.get("config") or data or {}
    except Exception:
        cfg = {}
    if not cfg:
        cfg = {
            "directories": {
                "data": tc.get("data_dir") or "",
                "user": sketchbook_dir(tc),
            }
        }
    cfg.setdefault("library", {})["enable_unsafe_install"] = True
    path = os.path.join(work_root(), "lib_install_config.yaml")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(_yaml_lines(cfg)) + "\n")
    except OSError:
        return []
    tc["_unsafe_config"] = path
    return ["--config-file", path]


def lib_install(cli_tc: dict, on_line=None, names=None, zip_paths=None,
                git_urls=None, no_deps: bool = False) -> int:
    """安装库：按名称 / ZIP / Git 仓库。"""
    names = [n for n in (names or []) if n.strip()]
    zip_paths = [p for p in (zip_paths or []) if p.strip()]
    git_urls = [u for u in (git_urls or []) if u.strip()]
    if not (names or zip_paths or git_urls):
        raise ValueError("请指定要安装的库（名称 / ZIP 文件 / Git 地址 / 本地文件夹）")
    cmd = [cli_tc["cli"], "lib", "install"]
    if zip_paths or git_urls:
        # zip / git 安装需要打开 enable_unsafe_install（用临时配置，不动用户配置）
        cmd += unsafe_install_config_args(cli_tc)
        if on_line:
            on_line("[信息] 已为本次安装启用 library.enable_unsafe_install（临时配置）")
    else:
        cmd += cli_tc.get("config_args", [])
    for path in zip_paths:
        if not os.path.isfile(path):
            raise FileNotFoundError(f"ZIP 文件不存在：{path}")
        cmd += ["--zip-path", path]
    for url in git_urls:
        cmd += ["--git-url", url]
    if no_deps:
        cmd.append("--no-deps")
    cmd += names
    rc = run_stream(cmd, on_line=on_line)
    if rc == 0:
        lib_list(cli_tc, force=True)
    return rc


def lib_install_folder(cli_tc: dict, folder: str, on_line=None, no_deps: bool = False) -> int:
    """从本地文件夹安装：先打包成 Arduino 库 zip（保留顶层目录），再安装。"""
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise FileNotFoundError(f"文件夹不存在：{folder}")
    has_meta = os.path.isfile(os.path.join(folder, "library.properties"))
    src_suffixes = (".h", ".hpp", ".c", ".cpp", ".cc", ".cxx", ".ino", ".S", ".s")
    has_src = any(f.lower().endswith(src_suffixes)
                  for _r, _d, files in os.walk(folder) for f in files)
    if not has_src:
        raise ValueError(f"该文件夹里没有 C/C++ 源文件，可能选错了目录：{folder}")
    if on_line:
        on_line(f"[信息] {'检测到 library.properties' if has_meta else '未发现 library.properties，仍按目录名打包'}")
        on_line("[信息] 正在打包成 zip …")
    zip_path = os.path.join(work_root(), f"lib_{os.path.basename(folder)}.zip")
    zip_library_folder(folder, zip_path)
    if on_line:
        on_line(f"[信息] 打包完成：{zip_path}（{os.path.getsize(zip_path)} 字节）")
    return lib_install(cli_tc, on_line=on_line, zip_paths=[zip_path], no_deps=no_deps)


def zip_library_folder(folder: str, out_zip: str) -> str:
    """把库文件夹打包成 Arduino 规范 zip（内含单层顶层目录）。"""
    import zipfile

    folder = os.path.abspath(folder)
    parent = os.path.dirname(folder)
    skip_dirs = {"__pycache__", ".git", "build", "node_modules"}
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for fname in files:
                full = os.path.join(root, fname)
                try:
                    zf.write(full, os.path.relpath(full, parent))
                except OSError:
                    continue
    return out_zip


def lib_uninstall(tc: dict, names, on_line=None) -> int:
    names = [n for n in (names or []) if n.strip()]
    if not names:
        raise ValueError("没有选择要卸载的库")
    rc = run_stream(_lib_cmd(tc, "uninstall", *names), on_line=on_line)
    if rc == 0:
        lib_list(tc, force=True)
    return rc


def lib_update_index(tc: dict, on_line=None) -> int:
    return run_stream(_lib_cmd(tc, "update-index"), on_line=on_line)


def lib_upgrade(tc: dict, names=None, on_line=None) -> int:
    args = ["upgrade"]
    if names:
        args += [n for n in names if n.strip()]
    rc = run_stream(_lib_cmd(tc, *args), on_line=on_line)
    if rc == 0:
        lib_list(tc, force=True)
    return rc


def parse_missing_headers(log_lines) -> list[str]:
    """从编译日志里找出缺失的头文件（用于提示该装哪个库）。"""
    pattern = re.compile(r"fatal error:\s*([\w./\\+-]+\.(?:h|hpp)):\s*No such file", re.I)
    missing = []
    for line in log_lines or []:
        for hit in pattern.findall(line or ""):
            name = os.path.basename(hit.replace("\\", "/"))
            if name not in missing:
                missing.append(name)
    return missing


# ---------------------------------------------------------------------------
# 环境探测汇总
# ---------------------------------------------------------------------------
def environment_report() -> dict:
    """探测本机可用的编译环境，供界面显示。"""
    toolchains = detect_toolchains(force=True)
    primary = toolchains[0] if toolchains else None
    cli = primary["cli"] if primary else find_arduino_cli()
    cores = arduino_cores(cli, config_args=primary["config_args"] if primary else None) if cli else []
    data_dir = primary["data_dir"] if primary else arduino_data_dir()
    idf = find_idf()
    return {
        "arduino_cli": cli,
        "arduino_data": data_dir,
        "cores": cores,
        "idf": idf,
        "toolchains": toolchains,
    }


def describe_environment(report: dict) -> list[str]:
    lines = []
    toolchains = report.get("toolchains")
    if toolchains is None:
        toolchains = detect_toolchains()
    lines.extend(describe_toolchains(toolchains))

    cli = report.get("arduino_cli")
    if cli:
        lines.append(f"当前优先使用的 arduino-cli：{cli}")
    else:
        lines.append("arduino-cli：未找到（需要 Arduino IDE 2.x 或单独安装 arduino-cli）")

    idf = report.get("idf")
    if idf:
        lines.append(f"ESP-IDF：{idf.get('root') or idf.get('idf_py')}")
    else:
        lines.append("ESP-IDF：未找到（仅在使用 ESP-IDF 工程时需要）")
    return lines
