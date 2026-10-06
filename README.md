> **本仓库 = 图形化 ESP 烧录器（Super_ESP_Tool）+ esptool 5.3.1 源码**
>
> * 图形界面工具（扫描/烧录/编译/库管理）文档见 **[README_ESP烧录器.md](README_ESP烧录器.md)**；
> * 本仓库**不含** `toolchain/` 目录（约 2.3 GB 的 Arduino 编译工具链），克隆后请自备 Arduino 环境，
>   详见该文档的“关于本仓库”一节；
> * 下面这段是上游 esptool 的原始说明。

# esptool

A Python-based, open-source, platform-independent serial utility for flashing, provisioning, and interacting with Espressif SoCs.

[![Test esptool](https://github.com/espressif/esptool/actions/workflows/test_esptool.yml/badge.svg?branch=master)](https://github.com/espressif/esptool/actions/workflows/test_esptool.yml) [![Build esptool](https://github.com/espressif/esptool/actions/workflows/build_esptool.yml/badge.svg?branch=master)](https://github.com/espressif/esptool/actions/workflows/build_esptool.yml)
[![pre-commit.ci status](https://results.pre-commit.ci/badge/github/espressif/esptool/master.svg)](https://results.pre-commit.ci/latest/github/espressif/esptool/master)

## Documentation

Visit the [documentation](https://docs.espressif.com/projects/esptool/) or run `esptool -h`.

## Flasher Stub

esptool uploads a small [flasher stub](https://docs.espressif.com/projects/esptool/en/latest/esptool/flasher-stub.html) program to the chip to improve flashing performance and work around ROM bootloader limitations. The stub is developed in the [esp-flasher-stub](https://github.com/espressif/esp-flasher-stub) repository. Prebuilt binaries are bundled with esptool releases.

## Contribute

If you're interested in contributing to esptool, please check the [contributions guide](https://docs.espressif.com/projects/esptool/en/latest/contributing.html).

## About

esptool was initially created by Fredrik Ahlberg (@[themadinventor](https://github.com/themadinventor/)), and later maintained by Angus Gratton (@[projectgus](https://github.com/projectgus/)). It is now supported by Espressif Systems. It has also received improvements from many members of the community.

## License

This document and the attached source code are released as Free Software under GNU General Public License Version 2 or later. See the accompanying [LICENSE file](https://github.com/espressif/esptool/blob/master/LICENSE) for a copy.
