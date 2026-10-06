#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ESP 烧录器无硬件自检脚本（演示模式）。

用法：  python gui_selftest.py
作用：  在 --demo 演示模式下构建界面、扫描端口、连接设备、检查烧录表格逻辑，
       不访问任何真实串口。全部通过时输出 "自检通过"。
"""

import os
import sys
import time

sys.argv = ["gui_selftest.py", "--demo"]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import esp_flasher_gui as g  # noqa: E402


def main() -> int:
    assert g.ESPTOOL_IMPORT_ERROR is None, f"esptool 导入失败: {g.ESPTOOL_IMPORT_ERROR}"
    assert g.DEMO_MODE
    g.DEMO_AUTORUN = False      # 自检手动驱动流程，关闭演示自动运行

    app = g.EspFlasherApp()
    app.withdraw()

    def pump(seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            app.update()
            time.sleep(0.02)

    # 1) 端口枚举
    pump(0.6)
    ports = app.tree_ports.get_children()
    assert len(ports) == 3, f"演示模式应有 3 个串口，实际 {len(ports)}"
    print(f"[1] 端口枚举 OK: {[app.tree_ports.item(i, 'values')[0] for i in ports]}")

    # 2) 扫描设备（芯片型号 / 状态）
    app._scan_devices()
    pump(3.0)
    table = [app.tree_ports.item(i, "values") for i in app.tree_ports.get_children()]
    for row in table:
        print(f"[2] {row[0]:<6} 型号={row[1]:<9} 状态={row[2]}")
    assert any(r[1] == "ESP32-S3" for r in table), "未识别出 ESP32-S3"
    assert any("无响应" in r[2] for r in table), "未标记无响应端口"
    assert not app.busy, "扫描结束后仍处于忙状态"

    # 3) 连接并读取设备信息
    app.var_port.set("COM7")
    app._connect_device()
    pump(2.0)
    info = [app.tree_info.item(i, "values") for i in app.tree_info.get_children()]
    keys = [r[0] for r in info]
    for need in ("芯片型号", "芯片描述", "芯片版本", "芯片特性", "MAC 地址",
                 "Flash 厂商", "Flash 容量", "安全启动", "Flash 加密", "串口"):
        assert need in keys, f"设备信息缺少字段: {need}"
    print(f"[3] 设备信息 OK: {len(info)} 项，芯片型号="
          f"{dict(info).get('芯片型号')}")

    # 4) 烧录表格与参数检查
    app._clear_flash_files()
    app.tree_files.insert("", "end", values=("demo.bin", "0x10000", "1 MB"))
    app.var_mode.set("single")
    app.var_single_file.set("")          # 空路径应报错
    try:
        app._collect_flash_targets()
        raise AssertionError("空文件路径未触发校验错误")
    except ValueError:
        pass
    app.var_mode.set("multi")
    app.tree_files.insert("", "end", values=("demo2.bin", "zz", "1 MB"))   # 地址非法
    try:
        app._collect_flash_targets()
        raise AssertionError("非法地址未触发校验错误")
    except ValueError:
        pass
    app._clear_flash_files()
    print("[4] 烧录参数校验 OK")

    # 5) 地址自动建议
    assert app._suggest_offset("bootloader.bin") == "0x0"
    assert app._suggest_offset("partition-table.bin") == "0x8000"
    assert app._suggest_offset("my_app.bin") == "0x10000"
    print("[5] 地址自动建议 OK")

    # 6) 日志重定向
    log_text = app.txt_log.get("1.0", "end")
    assert "启动" in log_text and "esptool" in log_text
    print(f"[6] 日志输出 OK: {len(log_text.splitlines())} 行")

    # 7) 演示烧录流程（进度条 + 任务状态机）
    app._clear_flash_files()
    app.tree_files.insert("", "end", values=("demo.bin", "0x10000", "1 MB"))
    targets = app._collect_flash_targets()
    assert targets == [(0x10000, "demo.bin")], targets
    app._start_worker("烧录固件", lambda: app._task_flash("COM7", targets))
    assert app.busy, "任务未标记为运行中"
    pump(3.0)
    assert not app.busy, "任务结束后未复位忙状态"
    assert app.progress["value"] > 99, f"进度条未走完: {app.progress['value']}"
    assert "烧录完成" in app.txt_log.get("1.0", "end")
    print(f"[7] 烧录流程 OK: 进度={app.progress['value']:.0f}%")

    # 8) 编译后端（源码 → bin）纯逻辑检查
    import esp_compile as ec

    assert ec.chip_from_fqbn("esp32:esp32:esp32s3") == "esp32s3"
    assert ec.chip_from_fqbn("esp32:esp32:esp32cam") == "esp32"
    assert ec.chip_from_fqbn("esp8266:esp8266:generic") == "esp8266"
    assert ec.bootloader_offset("esp32") == 0x1000
    assert ec.bootloader_offset("esp32s3") == 0x0
    assert ec.parse_addr("0x8000") == 0x8000 and ec.parse_addr("") == 0

    env_lines = ec.describe_environment(ec.environment_report())
    assert env_lines, "环境探测没有返回任何内容"
    print(f"[8] 编译环境探测 OK: arduino-cli="
          f"{'有' if ec.find_arduino_cli() else '无'}, "
          f"ESP-IDF={'有' if ec.find_idf() else '无'}")

    # 9) .cpp/.h 目录包装 + 产物清单 + 合并 + ELF→BIN
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="esp_gui_test_")
    src_dir = os.path.join(tmp, "myproj")
    os.makedirs(src_dir)
    with open(os.path.join(src_dir, "main.cpp"), "w", encoding="utf-8") as fh:
        fh.write("void setup() {}\nvoid loop() {}\n")
    with open(os.path.join(src_dir, "helper.h"), "w", encoding="utf-8") as fh:
        fh.write("#pragma once\n")
    sketch, wrapped = ec.wrap_sources_as_sketch(src_dir)
    assert wrapped, "只有 .cpp/.h 的目录应当被包装成 sketch"
    assert os.path.isfile(os.path.join(sketch, "myproj.ino")), "缺少自动生成的 .ino"
    assert os.path.isfile(os.path.join(sketch, "main.cpp")), "没有复制 .cpp"
    # 已有 .ino 的目录应原样返回
    with open(os.path.join(src_dir, "myproj.ino"), "w", encoding="utf-8") as fh:
        fh.write("// sketch\n")
    sketch2, wrapped2 = ec.wrap_sources_as_sketch(src_dir)
    assert not wrapped2 and sketch2 == os.path.abspath(src_dir), "有 .ino 时不应重新包装"

    out_dir = os.path.join(tmp, "out")
    os.makedirs(out_dir)
    for name, size in (("x.ino.bin", 4096), ("x.ino.bootloader.bin", 1024),
                       ("x.ino.partitions.bin", 512)):
        with open(os.path.join(out_dir, name), "wb") as fh:
            fh.write(b"\xe9" + b"\x00" * (size - 1))
    items = ec.collect_arduino_items(out_dir, "esp32:esp32:esp32", None)
    assert [a for a, _p in items] == [0x1000, 0x8000, 0x10000], items
    factory = os.path.join(out_dir, "factory.bin")
    ec.merge_bins(items, "esp32", factory)
    data = open(factory, "rb").read()
    assert data[0x1000] == 0xE9 and data[0x10000] == 0xE9 and data[0x8000] == 0xE9
    assert data[:0x1000] == b"\xff" * 0x1000, "低地址应填充 0xFF"
    print(f"[9] 合并 BIN OK: factory.bin={len(data)} 字节")

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "test"))
    try:
        from elf_builder import build_elf

        elf_path = os.path.join(tmp, "fake.elf")
        build_elf(elf_path, "xtensa", [(".flash.text", 0x400D0020, 64, "rx")],
                  entry=0x400D0020)
        bin_path = os.path.join(tmp, "fake.bin")
        ec.elf_to_bin(elf_path, "esp32", bin_path, flash_mode="dio",
                      flash_size="4MB", flash_freq="40m")
        blob = open(bin_path, "rb").read()
        assert blob[0] == 0xE9, "elf2image 输出不是有效镜像"
        print(f"[10] ELF→BIN OK: {len(blob)} 字节, entry="
              f"{hex(int.from_bytes(blob[4:8], 'little'))}")
    except ImportError:
        print("[10] 跳过 ELF→BIN（缺少 test/elf_builder.py）")

    # 11) 编译产物自动填入烧录页
    app._apply_compiled({"items": [("/tmp/a.bin", "0x10000", "1 MB"),
                                   ("/tmp/b.bin", "0x8000", "3 KB")],
                         "merged": None, "out_dir": "/tmp"})
    assert app.var_mode.get() == "multi"
    assert len(app.tree_files.get_children()) == 2
    app._apply_compiled({"items": [], "merged": factory, "out_dir": out_dir})
    assert app.var_mode.get() == "single"
    assert app.var_single_file.get() == factory and app.var_single_addr.get() == "0x0"
    print("[11] 编译产物填充烧录页 OK")

    # 12) 按已连接设备自动匹配（芯片识别 → 工具链 → 开发板 → 各页芯片）
    assert ec.chip_from_text("ESP32-C6") == "esp32c6", "带连字符的芯片名没有被归一化"
    assert ec.chip_from_text("ESP32-S3") == "esp32s3"
    assert ec.chip_from_text("ESP32") == "esp32"
    toolchains = ec.detect_toolchains()
    assert toolchains, "没有探测到任何 Arduino 工具链"
    for line in ec.describe_toolchains(toolchains):
        print("     ", line)
    if toolchains:
        c6_tc = ec.resolve_toolchain(chip="esp32c6")
        s3_tc = ec.resolve_toolchain(chip="esp32s3")
        assert c6_tc and "esp32c6" in c6_tc["chips"], "没有工具链能编译 ESP32-C6"
        assert s3_tc and "esp32s3" in s3_tc["chips"], "没有工具链能编译 ESP32-S3"
        c6_boards = ec.boards_for_chip("esp32c6", toolchain=c6_tc)
        assert c6_boards and c6_boards[0] == "esp32:esp32:esp32c6", c6_boards[:3]

        app.var_auto_match.set(True)
        app.conn_chip = "ESP32-C6"
        app.var_c_fqbn.set("esp32:esp32:esp32")        # 故意设错，看是否被纠正
        app._auto_match_device("ESP32-C6", flash_size="4MB")
        assert app.var_c_fqbn.get() == "esp32:esp32:esp32c6", app.var_c_fqbn.get()
        assert app.var_i_target.get() == "esp32c6"
        assert app.var_e_chip.get() == "esp32c6"
        assert app.var_m_chip.get() == "esp32c6"
        assert app.var_flash_size.get() == "4MB"
        assert "ESP32-C6" in app.lbl_match.cget("text")

        # 关闭自动匹配后，连接事件不应再改动编译设置
        app.var_auto_match.set(False)
        app.var_c_fqbn.set("esp32:esp32:esp32s3")
        app._handle_event({"t": "device", "rows": [("— 提示 —", "")],
                           "meta": {"chip": "ESP32-C6"}})
        assert app.var_c_fqbn.get() == "esp32:esp32:esp32s3", "关闭自动匹配后仍被改动"
        app.var_auto_match.set(True)
        print(f"[12] 自动匹配 OK: esp32c6→{c6_tc['label']}, esp32s3→{s3_tc['label']}")
    else:
        print("[12] 跳过自动匹配（未找到工具链）")

    # 13) 库管理：缺失头文件识别 + 库目录 + 隔离环境下真实装/卸
    assert ec.parse_missing_headers(
        ["a.cpp:1:10: fatal error: Foo.h: No such file or directory",
         "a.cpp:2:10: fatal error: Bar.h: No such file or directory",
         "a.cpp:3:10: fatal error: Foo.h: No such file or directory"]) == ["Foo.h", "Bar.h"]

    tc = ec.library_toolchain()
    real_dir = ec.library_dir(tc)
    before = set(os.listdir(real_dir)) if os.path.isdir(real_dir) else set()
    print(f"[13] 库目录 OK: {real_dir}")

    tmp2 = tempfile.mkdtemp(prefix="esp_gui_lib_")
    user_dir = os.path.join(tmp2, "Arduino")
    os.makedirs(user_dir, exist_ok=True)
    cfg = os.path.join(tmp2, "cli.yaml")
    with open(ec.BUNDLED_ARDUINO_CONFIG, encoding="utf-8") as fh, \
            open(cfg, "w", encoding="utf-8") as out:
        for line in fh:
            out.write(f"  user: {user_dir.replace(os.sep, '/')}\n"
                      if line.strip().startswith("user:") else line)
    iso = dict(tc)
    iso["config_args"] = ["--config-file", cfg]
    iso.pop("_unsafe_config", None)
    assert ec.library_dir(iso).startswith(tmp2), "隔离配置没有生效"

    lib_src = os.path.join(tmp2, "ZZSelfTestLib")
    os.makedirs(os.path.join(lib_src, "src"))
    with open(os.path.join(lib_src, "library.properties"), "w", encoding="utf-8") as fh:
        fh.write("name=ZZSelfTestLib\nversion=3.0.1\nauthor=selftest\n"
                 "architectures=*\nsentence=selftest\n")
    with open(os.path.join(lib_src, "src", "ZZSelfTestLib.h"), "w", encoding="utf-8") as fh:
        fh.write("#pragma once\n")

    rc = ec.lib_install_folder(iso, lib_src)
    assert rc == 0, f"从文件夹安装库失败 rc={rc}"
    installed = [l["name"] for l in ec.lib_list(iso, force=True)]
    assert "ZZSelfTestLib" in installed, installed
    rc = ec.lib_uninstall(iso, ["ZZSelfTestLib"])
    assert rc == 0, f"卸载库失败 rc={rc}"
    assert "ZZSelfTestLib" not in [l["name"] for l in ec.lib_list(iso, force=True)]

    after = set(os.listdir(real_dir)) if os.path.isdir(real_dir) else set()
    assert after == before, f"隔离测试影响了真实库目录：{after ^ before}"
    shutil.rmtree(tmp2, ignore_errors=True)
    print("[14] 库安装/卸载 OK（隔离环境，未改动真实库目录）")

    shutil.rmtree(tmp, ignore_errors=True)
    app.destroy()
    print("\n自检通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
