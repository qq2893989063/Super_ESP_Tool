#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""编译功能端到端测试：真实调用 arduino-cli，把 .cpp/.h 编译成 .bin。

用法：  python compile_e2e_test.py [FQBN]

* 默认 FQBN 为 ``esp32:esp32:esp32``（需要已安装 esp32 Arduino 核心）；
* 未检测到 arduino-cli 或对应核心时会自动跳过（退出码 0）；
* 测试会走完整链路：包装 .cpp/.h → arduino-cli 编译 → 收集产物 → 合并 factory.bin
  → 自动填入“烧录固件”页，并校验生成文件的镜像头。

首次编译比较慢（要编译 Arduino 核心），之后就快了。
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FQBN = sys.argv[1] if len(sys.argv) > 1 else "esp32:esp32:esp32"

MAIN_CPP = """\
#include <Arduino.h>
#include "helper.h"

static const int kLedPin = 2;

void setup() {
  Serial.begin(115200);
  Serial.println(greeting());
  pinMode(kLedPin, OUTPUT);
}

void loop() {
  digitalWrite(kLedPin, !digitalRead(kLedPin));
  delay(blink_delay());
}
"""

HELPER_H = """\
#pragma once
#include <Arduino.h>
const char *greeting();
int blink_delay();
"""

HELPER_CPP = """\
#include "helper.h"
const char *greeting() { return "hello from cpp+h"; }
int blink_delay() { return 500; }
"""


def main() -> int:
    import esp_compile as ec

    cli = ec.find_arduino_cli()
    if not cli:
        print("跳过：未找到 arduino-cli")
        return 0
    core_id = ":".join(FQBN.split(":")[:2])
    installed = {c["id"] for c in ec.arduino_cores(cli)}
    if core_id not in installed:
        print(f"跳过：未安装 Arduino 核心 {core_id}（已安装：{sorted(installed)}）")
        return 0
    print(f"arduino-cli : {cli}")
    print(f"目标开发板  : {FQBN}")

    import esp_flasher_gui as g

    work = tempfile.mkdtemp(prefix="esp_compile_e2e_")
    src_dir = os.path.join(work, "blink_cpp")
    os.makedirs(src_dir)
    for name, content in (("main.cpp", MAIN_CPP), ("helper.h", HELPER_H),
                          ("helper.cpp", HELPER_CPP)):
        with open(os.path.join(src_dir, name), "w", encoding="utf-8") as fh:
            fh.write(content)
    out_dir = os.path.join(work, "build_out")
    print(f"源码目录    : {src_dir}（只有 .cpp/.h，没有 .ino）")

    app = g.EspFlasherApp()
    app.withdraw()

    def pump(seconds: float, until=None) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            app.update()
            time.sleep(0.02)
            if until is not None and until():
                return True
        return until() if until else True

    pump(20, until=lambda: "arduino-cli" in app.lbl_compile_env.cget("text"))

    app.var_compile_mode.set("arduino")
    app._update_compile_mode()
    app.var_c_src.set(src_dir)
    app.var_c_fqbn.set(FQBN)
    app.var_c_out.set(out_dir)
    app.var_c_merge.set(True)

    started = time.time()
    app._start_compile()
    assert app.busy, "编译任务没有启动"
    print("编译中…（首次编译需要几分钟）")
    ok = pump(900, until=lambda: not app.busy)
    elapsed = time.time() - started
    assert ok, "编译超时"

    status = app.lbl_status.cget("text")
    print(f"编译耗时    : {elapsed:.1f} 秒")
    print(f"状态        : {status} | {app.lbl_detail.cget('text')}")
    if "编译完成" not in status:
        print("日志末尾：")
        for line in app.txt_log.get("1.0", "end").strip().splitlines()[-15:]:
            print("   ", line)
        app.destroy()
        return 1

    factory = os.path.join(out_dir, "factory.bin")
    assert os.path.isfile(factory), f"没有生成 {factory}"
    blob = open(factory, "rb").read()
    # bootloader 偏移随芯片不同（ESP32 在 0x1000，S3/C3/C6 在 0x0）
    boot_addr = ec.bootloader_offset(ec.chip_from_fqbn(FQBN))
    assert blob[boot_addr] == 0xE9, f"{boot_addr:#x} 处不是镜像（bootloader 偏移不对？）"
    assert blob[0x10000] == 0xE9, "0x10000 处不是应用镜像"
    print(f"factory.bin : {len(blob)} 字节，镜像头校验通过"
          f"（bootloader@{boot_addr:#x}、app@0x10000）")

    assert app.var_mode.get() == "single", "产物没有填入烧录页"
    assert app.var_single_file.get() == factory
    assert app.var_single_addr.get() == "0x0"
    print("烧录页      : 已自动填入 factory.bin @0x0")
    print("\n端到端编译测试通过 ✔")
    app.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
