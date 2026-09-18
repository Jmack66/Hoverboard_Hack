# Hoverboard Sideboard / Motor Controller Hack

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

Custom firmware and a full reverse-engineering toolchain for a hoverboard motor
controller PCB silk-screened **GD32F130K6** that is actually a
**MM32SPIN05PF** (MindMotion) in disguise. The chip deception, wiring,
register model, and every bug hit along the way are written up in
**[DEVLOG.md](DEVLOG.md)** — start there.

This started from [EFeru/hoverboard-sideboard-hack-GD](https://github.com/EFeru/hoverboard-sideboard-hack-GD)
(GPLv3) as a base for the sideboard firmware structure (`Src/`, `Drivers/`,
`MDK-ARM/`), but the motor-controller work, flashing tooling, and
`platformio.ini` build target here are project-specific and diverge
significantly from that upstream repo.

---
Table of Contents
=======================

* [What's Actually Here](#whats-actually-here)
* [The Chip Deception (short version)](#the-chip-deception-short-version)
* [Flashing](#flashing)
* [Reference Docs](#reference-docs)
* [License](#license)

---
## What's Actually Here

- **`DEVLOG.md`** — the real documentation: chip ID, toolchain setup, wiring,
  pin assignments, clock/UART/GPIO/TIM1 register model, hall sensor +
  commutation table, bugs found and fixed, UART command interface, and the
  full flash unlock/erase/program procedure.
- **`Src/`, `Drivers/`, `MDK-ARM/`, `platformio.ini`, `Makefile`** — firmware
  source, targeting the `K6_motor` PlatformIO environment (Cortex-M0, GD32
  SPL headers where they still apply, MM32-specific peripheral handling
  elsewhere).
- **Flashing / unlock tooling** (all built to work around the fact that this
  chip lies about what it is to every standard tool):
  - `flash_gd32.py` — flashes via OpenOCD telnet using 32-bit word DAP writes
    (works around the stm32f1x driver failing to probe GD32, and a picoprobe
    byte-lane bug).
  - `mm32_pyocd_flash.py` — flashes via pyOCD's built-in FLM flash loader.
  - `mm32_direct_flash.py` / `mm32_sram_flash.py` — direct/SRAM-stub-based
    halfword programming, bypassing pyOCD's FLM when the CPU isn't running.
  - `mm32_rdp_clear.py` / `mm32_clear_rdp.py` — clear flash read protection
    (RDP) via an SRAM stub; required before any write will succeed on a
    protected chip.
  - `mm32_unlock_and_flash.py` — combined RDP-clear + flash sequence.
  - `mm32_diag.py` — post-flash live diagnostic (halts CPU, dumps key
    registers).
  - `flash_stub.c` / `stub.c` (+ built `stub.bin` / `stub.elf`) — tiny
    SRAM-resident programmer stubs used by the scripts above.
  - `hall_scan.py` / `hall_scan_api.py` — scans GPIOA/B/C IDR for toggling
    hall sensor bits while the motor is spun by hand.
  - `create_reg_files.sh` — scaffolds MM32 HAL register header stubs (paths
    inside are local to the author's machine; treat as a template).
  - `ref_fw.bin`, `main_c_v1.txt`, `main_c_v2.txt` — known-good reference
    firmware dump and source snapshots used to cross-check register
    behaviour during reverse engineering.
- **`hoverboard_hack_esp32_manualspeed-main/`** — ESP32 sketch that drives the
  hoverboard over the serial `hover|<motor>|<speed>|<state>` protocol
  (single motor, left/right, or all). Based on
  [RoboDurden/Hoverboard-Firmware-Hack-Gen2.x-GD32](https://github.com/RoboDurden/Hoverboard-Firmware-Hack-Gen2.x-GD32/tree/main/Arduino%20Examples/TestSpeed);
  ADC-based potentiometer control is present but untested.

## The Chip Deception (short version)

The board and its silk-screen say GD32F130K6. It is not. It's an
MM32SPIN05PF: same Cortex-M0 core, but STM32F1-style GPIO registers
(CRL/CRH) instead of the GD32/STM32F0 MODER/OTYPER layout, different
clock/PLL behaviour, and a debug probe that reports it correctly as
`MM32SPIN05P` if you bother to ask. Full details, including how it was
confirmed and every downstream consequence, are in
[DEVLOG.md §1](DEVLOG.md#1-the-chip-deception).

## Flashing

Don't use the standard OpenOCD `flash write_image erase` — it fails because
the MM32SPIN05PF doesn't present a valid STM32 device ID. Use pyOCD, and if
the chip is locked/bricked, clear RDP first. The full step-by-step (OpenOCD
+ telnet session, register pokes, page addresses) is in
[DEVLOG.md §15](DEVLOG.md#15-flash-unlock-erase-and-program-procedure-mm32spin05pf).

PlatformIO is configured for the `K6_motor` environment (see
`platformio.ini`); `upload_command` already points at
`pyocd flash -t mm32spin05pf`.

## Reference Docs

`docs/` carries vendor reference material used throughout this work:
GD32F1x0 and GD32F130xx datasheets/firmware libraries, MPU-6000/6050
datasheets and register maps (for sideboard IMU variants), and pinout
diagrams under `docs/pictures/`.

## License

GPLv3, inherited from the upstream project this was based on — see
[LICENSE](LICENSE). If you want to support the original sideboard firmware
this was forked from, see [EFeru/hoverboard-sideboard-hack-GD](https://github.com/EFeru/hoverboard-sideboard-hack-GD).
