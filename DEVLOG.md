# MM32SPIN05PF Hoverboard Motor Controller — Development Log

A complete reference for hacking the hoverboard motor driver PCB that is physically labelled
**GD32F130K6** but actually contains an **MM32SPIN05PF** (MindMotion).  
Documents the full toolchain, chip quirks, wiring, and every bug we hit.

---

## Table of Contents

1. [The Chip Deception](#1-the-chip-deception)
2. [Toolchain Setup](#2-toolchain-setup)
3. [Hardware Wiring](#3-hardware-wiring)
4. [Pin Assignments](#4-pin-assignments)
5. [Clock Initialisation](#5-clock-initialisation)
6. [UART Configuration](#6-uart-configuration)
7. [GPIO Register Model](#7-gpio-register-model)
8. [TIM1 Complementary PWM](#8-tim1-complementary-pwm)
9. [Hall Sensors and Commutation Table](#9-hall-sensors-and-commutation-table)
10. [Critical Bugs Found and Fixed](#10-critical-bugs-found-and-fixed)
11. [UART Command Interface](#11-uart-command-interface)
12. [Motor Phase Order](#12-motor-phase-order)
13. [Reference Firmware](#13-reference-firmware)

---

## 1. The Chip Deception

The PCB silk-screen and every label says **GD32F130K6** (LQFP32).  
The actual silicon is an **MM32SPIN05PF** (LQFP48, MindMotion).

Consequences:
- GD32 SPL headers (`gd32f1x0.h`) reference the wrong peripheral register layouts —
  GPIO, UART, and clock registers are all different.
- The GD32 `SystemInit` tries to lock a PLL via `RCU_CFG0`; on MM32 those writes are
  ignored and `PLLRDY` never sets → the chip hangs before `main()`.
- The chip core is Cortex-M0, compatible with `-mcpu=cortex-m0`.
- The MM32SPIN05PF uses **STM32F1-style** GPIO registers (4-bit CRL/CRH per pin),
  not the GD32/STM32F0-style MODER/OTYPER layout.

The board was identified by:
- CMSIS-DAP probe reporting it as `MM32SPIN05P` on connect.
- Cross-referencing against the [MindMotion MM32SPIN05PF datasheet](https://www.mindmotion.com.cn/products/mm32mcu/mm32spin/).
- Comparing register dumps from a working reference firmware
  (`HoverboardOutputMM32SPIN05.hex`).

**Key takeaway**: never trust the MCU label on cheap hoverboard boards. Always
confirm with a debug probe before writing any peripheral code.

---

## 2. Toolchain Setup

### Build

[PlatformIO](https://platformio.org/) with the GD32 community platform (used only for
its GCC ARM cross-compiler and linker script — SPL peripheral libraries are bypassed for
anything MM32-specific).

```
platform = https://github.com/CommunityGD32Cores/platform-gd32.git#e712045bdba54790ed2e07c47a421345340efb31
framework = spl
board     = genericGD32F130C6
build_flags = -mcpu=cortex-m0 -mthumb -DUSE_STDPERIPH_DRIVER
build_unflags = -mcpu=cortex-m3
```

Build command:
```
cd /path/to/project && pio run
```

Output binary: `.pio/build/K6_motor/firmware.bin`

### Flash

[pyOCD](https://pyocd.io/) with the MindMotion DFP pack.

```
pyocd flash -t mm32spin05pf --pack /path/to/mm32_dfp_pack.zip firmware.bin
```

**DFP pack**: `MindMotion.MM32SPIN0x_DFP.1.0.8.pack` (rename to `.zip`).  
Download from the MindMotion website or their GitHub.  
The pack is needed because pyOCD does not bundle MM32 target descriptions.

### Debug probe

Raspberry Pi **Debugprobe** running the CMSIS-DAP firmware.
Connects to the board's 4-pin SWD header: `GND, SWCLK, SWDIO, VCC/3V3`.

The board can also be powered via its main power connector during debugging; in that case
leave the VCC pin on the SWD header disconnected to avoid conflicts.

### IDE false positives

VS Code / Clangd reports `'gd32f1x0.h' file not found` and undeclared identifiers because
clang does not have the ARM cross-toolchain headers. These are **false positives**.  
`pio run` compiles correctly. Ignore all clang diagnostics.

---

## 3. Hardware Wiring

### SWD debug header (on PCB)

```
[ GND | SWCLK | SWDIO | 3V3 ]
```

Connect to Debugprobe / ST-Link / J-Link with matching labels.  
If powering the board separately (recommended), do not connect 3V3 from the probe.

### ESP32 → Motor board UART

```
ESP32 TX  ──────── PB4  (board UART RX, AF3)
ESP32 RX  ──────── PB6  (board UART TX, AF0)
ESP32 GND ──────── GND  (board)
```

Settings: 115200 baud, 8N1.

The ESP32 sends 3-byte frames:
```
[0xAB] [int8_t speed_pct  −100..+100] [XOR checksum = 0xAB ^ speed_byte]
```
The board also accepts single ASCII bytes for bench testing (see §11).

### Motor phases

3-wire brushless (hub motor). Wires labelled A, B, C (or Yellow, Blue, Green).

```
Motor wire A ─── Gate driver output CH1  (PA8 hi-side / PB13 lo-side)
Motor wire B ─── Gate driver output CH2  (PA9 hi-side / PB14 lo-side)
Motor wire C ─── Gate driver output CH3  (PA10 hi-side / PB15 lo-side)
```

**Important**: the phase order must be confirmed empirically.
If the motor runs in the wrong direction or jitters, swap any two motor wires.
There are only 3 permutations to try. See §12.

### Hall sensors

The hub motor has 3 Hall effect sensors (active-low; board has external pull-ups).

```
Hall sensor wire VCC ── 5 V or 3V3 (check your motor spec)
Hall sensor wire GND ── GND
Hall sensor wire A   ── PC15
Hall sensor wire B   ── PC13
Hall sensor wire C   ── PC14
```

Hall signals are read raw (0 when magnet detected, 1 otherwise).  
Do NOT invert them in firmware — the STEP_FWD table is built for the raw convention.

### Power latch

**PB2 must be driven HIGH** immediately at startup or the board loses power when running
from battery. The firmware does this in `gpio_config()`.

---

## 4. Pin Assignments

| Signal | Pin | AF | Notes |
|--------|-----|----|-------|
| TIM1 CH1 (hi-A)  | PA8  | AF2 | High-side phase A |
| TIM1 CH2 (hi-B)  | PA9  | AF2 | High-side phase B |
| TIM1 CH3 (hi-C)  | PA10 | AF2 | High-side phase C |
| TIM1 CH1N (lo-A) | PB13 | AF2 | Low-side phase A |
| TIM1 CH2N (lo-B) | PB14 | AF2 | Low-side phase B |
| TIM1 CH3N (lo-C) | PB15 | AF2 | Low-side phase C |
| UART1 TX         | PB6  | AF0 | To ESP32 RX |
| UART1 RX         | PB4  | AF3 | From ESP32 TX |
| Hall A            | PC15 | —  | Active-low, floating input |
| Hall B            | PC13 | —  | Active-low, floating input |
| Hall C            | PC14 | —  | Active-low, floating input |
| Power latch       | PB2  | —  | Output HIGH to keep board on |

**Wrong pins that appear in GD32 documentation for this footprint (do not use):**
- PA7 is NOT TIM1 CH1N → it is PB13
- PB0 is NOT TIM1 CH2N → it is PB14
- PB1 is NOT TIM1 CH3N → it is PB15

---

## 5. Clock Initialisation

The GD32 default `SystemInit` hangs on MM32 because it tries to configure a PLL that
does not exist on this chip.

**Solution**: replace `system_gd32f1x0.c` with a custom `system_mm32.c` that defines
`SystemInit` as a strong (non-weak) symbol — the linker will prefer it over the SPL
archive copy.

### MM32 clock sequence

```
Reset default: CFGR_SW=00 → HSI/6 ≈ 12 MHz
Full speed:    CFGR_SW=10 → HSI 72 MHz (bit 20 of RCC_CR enables full-speed HSI)
```

**Critical**: set **2 flash wait states** (`FMC_WS[2:0] = 2`) BEFORE switching to 72 MHz.
Without this, flash timing violations cause completely random behaviour — garbled UART,
constant noise, erratic execution.

```c
// system_mm32.c  (verbatim working sequence)
FMC_WS = (FMC_WS & ~0x7U) | 2U;       // 2 wait states first
RCC_CR   |= (1U << 20);                 // enable 72 MHz HSI
RCC_CFGR  = (RCC_CFGR & ~0x3U) | 0x2U; // SW = 10
while ((RCC_CFGR & 0xCU) != 0x8U);    // wait SWS = 10
```

Register addresses:
```
RCC_CR   = 0x40021000
RCC_CFGR = 0x40021004
FMC_WS   = 0x40022000
```

Confirmed from reference firmware dump: `RCC_CR=0x00105903`, `CFGR=0x0A`, `FMC_WS=0x3A`.

### Derived constants at 72 MHz

| Constant | Value | Purpose |
|----------|-------|---------|
| SysTick LOAD | 71999 | 1 ms tick |
| PWM_PERIOD   | 1800  | 72 MHz / (2 × 20 kHz), center-aligned |
| BRR/FRA      | 39/1  | 72 MHz / 625 = 115200 baud (exact) |
| Dead time    | 72    | DTG = 72 counts ≈ 1 µs |

---

## 6. UART Configuration

MM32SPIN05PF has a completely different UART register layout to GD32 USART. Do not use any
GD32 USART SPL functions.

**UART1 base address**: `0x40013800` (same bus address as GD32 USART0, but different map).

| Register | Offset | Purpose |
|----------|--------|---------|
| TDR | +0x00 | Transmit data |
| RDR | +0x04 | Receive data |
| CSR | +0x08 | Status (RXAVL bit 1, TXFULL bit 2) |
| GCR | +0x18 | Enable: bit0=UARTEN, bit3=RX, bit4=TX |
| CCR | +0x1C | Format: bits[5:4]=11 for 8-bit |
| BRR | +0x20 | Integer baud divisor |
| FRA | +0x24 | Fractional baud × 16 |

**GCR must be exactly `0x19`** (`UARTEN | TX | RX`). Do not set bit 1 — it inverts the TX
signal, producing a constant noise stream instead of data.

Baud rate formula: `actual_baud = SYSCLK / (BRR × 16 + FRA)`  
For 115200 @ 72 MHz: `BRR=39, FRA=1` → divisor=625 → exactly 115200.

Poll-receive pattern (no interrupt needed for bench use):
```c
if (UART1_CSR & (1U << 1))   // RXAVL
    uint8_t b = (uint8_t)UART1_RDR;
```

---

## 7. GPIO Register Model

MM32SPIN05PF uses **STM32F1-style** GPIO (same as STM32F103), not the GD32/STM32F0 MODER
model. Every GD32 SPL GPIO call (`gpio_mode_set`, `gpio_af_set`, etc.) is wrong.

Use direct register writes:

```
CRL  (+0x00) — pins 0-7,  4 bits each
CRH  (+0x04) — pins 8-15, 4 bits each
IDR  (+0x08) — input data register
ODR  (+0x0C) — output data register
BSRR (+0x10) — bit set/reset
AFRL (+0x20) — AF select pins 0-7  (4 bits each, same encoding as STM32F0/GD32)
AFRH (+0x24) — AF select pins 8-15
```

**Do not write to offset +0x1C** — it is a different register on MM32 and corrupts UART.

CRL/CRH 4-bit field encoding:

| Value | Mode | CNF | Meaning |
|-------|------|-----|---------|
| 0x4 | 00 (input) | 01 | Floating input |
| 0x8 | 00 (input) | 10 | Input with pull |
| 0x1 | 01 (50 MHz) | 00 | Output push-pull |
| 0xB | 11 (50 MHz) | 10 | AF push-pull |

GPIO base addresses:
```
GPIOA = 0x48000000
GPIOB = 0x48000400
GPIOC = 0x48000800
```

---

## 8. TIM1 Complementary PWM

TIM1 base address: `0x40012C00`

### Working configuration

```c
TIM1_CR1   = 0;                              // stop while configuring
TIM1_CR2   = 0;                              // CCPC=0 (see bug §10.3)
TIM1_CCMR1 = 0x6868U;                        // CH1+CH2: OCM=PWM1, preload on
TIM1_CCMR2 = 0x0068U;                        // CH3: OCM=PWM1, preload on
TIM1_CCER  = 0;                              // all outputs off
TIM1_PSC   = 0;                              // no prescaler
TIM1_ARR   = 1799;                           // 20 kHz center-aligned
TIM1_BDTR  = (1<<15)|(1<<14)|(1<<11)|(1<<10)|72; // MOE+AOE+OSSR+OSSI+DTG≈1µs
TIM1_EGR   = 1;                              // UG: latch ARR into shadow
TIM1_CR1   = (3U<<5)|(1U<<7)|(1U<<2)|1U;    // CMS=11, ARPE, URS, CEN
```

### BDTR fields

| Bit | Name | Value | Meaning |
|-----|------|-------|---------|
| 15 | MOE  | 1 | Main output enable |
| 14 | AOE  | 1 | Auto-enable on update event |
| 11 | OSSR | 1 | Disabled outputs driven LOW when idle |
| 10 | OSSI | 1 | Disabled outputs driven LOW when idle |
| 7:0 | DTG | 72 | Dead time ≈ 1 µs at 72 MHz |

OSSR=OSSI=1 is important for safety: a disabled phase is held LOW (not floating).

### Output mode logic (the hardest part)

For each phase there are three modes:

| Mode | CCxE | CCxNE | OCM | Result |
|------|------|-------|-----|--------|
| +1 (PWM drive)   | 1 | 0 | 0x6 (PWM1) | High-side switches with PWM duty |
| -1 (return path) | 0 | 1 | **0x5** (forced-active) | Low-side fully ON |
| 0  (float)       | 0 | 0 | — | Both sides OFF |

**Why OCM=0x5 and not 0x4 for the grounded phase** (this was the critical bug):

With `CCxE=0, CCxNE=1`:  `OC1N = OCxREF XOR CCxNP`

- OCM=0x4 (forced-inactive): OCxREF=0 → OC1N=0 → low-side OFF → **no return path**
- OCM=0x5 (forced-active):   OCxREF=1 → OC1N=1 → low-side ON  → **current can flow**

---

## 9. Hall Sensors and Commutation Table

Hall sensors on PC13, PC14, PC15. Active-low (0 = magnet present).

```c
uint8_t read_hall(void) {
    uint32_t inc = GPIOC_IDR;
    uint8_t a = (inc >> 15) & 1U;  // PC15
    uint8_t b = (inc >> 13) & 1U;  // PC13
    uint8_t c = (inc >> 14) & 1U;  // PC14
    return (c << 2) | (b << 1) | a;
}
```

Do NOT invert the readings (`!`). The STEP_FWD table below is correct for raw
(non-inverted) readings matching the reference firmware convention.

### STEP_FWD commutation table

Indexed by raw hall state (1–6). mode: +1=PWM drive, -1=ground (return), 0=float.

```c
static const step_t STEP_FWD[8] = {
    { 0,  0,  0},  // 0: invalid
    {+1, -1,  0},  // 1: A+  B-
    { 0, +1, -1},  // 2: B+  C-
    {+1,  0, -1},  // 3: A+  C-
    {-1,  0, +1},  // 4: C+  A-
    { 0, -1, +1},  // 5: C+  B-
    {-1, +1,  0},  // 6: B+  A-
    { 0,  0,  0},  // 7: invalid
};
```

Reverse direction: negate all three fields of the looked-up step.

### Verifying hall wiring

Send `h` command while spinning wheel by hand. You should see the hall state cycle
cleanly through `1 → 3 → 2 → 6 → 4 → 5 → 1` (or its mirror in reverse).
Gaps or repeats indicate a wiring fault or faulty sensor.

---

## 10. Critical Bugs Found and Fixed

### Bug 1 — OCM=0x4 (forced-inactive) kills all current flow

**Symptom**: motor never moves at all. Not even a twitch. `o` open-loop command silent.

**Root cause**: the grounded phase (mode=-1) used `OCM=0x4` (forced-inactive).  
With `CCxE=0, CCxNE=1`: `OC1N = OCxREF XOR CCxNP = 0 → LOW → low-side ALWAYS OFF`.  
No current return path through any phase → zero torque in every commutation step.

**Fix**: change to `OCM=0x5` (forced-active) → `OCxREF=1 → OC1N=1 → low-side ON`.

```c
// WRONG:
uint32_t ocm = (mode == -1) ? 0x4U : 0x6U;

// CORRECT:
uint32_t ocm = (mode == -1) ? 0x5U : 0x6U;
```

This was the single most impactful bug: it silently made every commutation step produce
zero torque, masking all other issues.

### Bug 2 — Hall sensor inversion

**Symptom**: motor tries to spin but fights itself or spins in wrong direction.

**Root cause**: `read_hall()` inverted pin readings with `!`.  
`our_hall = 7 - ref_hall` → every hall state maps to the reverse-direction commutation
step. Combined with Bug 1, the effect was invisible (zero torque anyway).

**Fix**: remove `!` inversion. Read raw pin level. 0 = sensor active.

### Bug 3 — CCPC=1 breaks direct CCER writes

**Symptom**: `t` test command (`TIM1_CCER = 0x0555`) produced no output.
Debug dump showed CCER still 0 after the write.

**Root cause**: with `CCPC=1` in `TIM1_CR2`, writes to CCER go to a shadow register that
only transfers on a COMG event (`TIM1_EGR bit 5`). The `t` test never sent COMG.
Also: `apply_channel` did a read-modify-write of CCER, reading the stale active register
instead of the shadow, accumulating wrong bits across 3 channel writes.

**Fix**: set `TIM1_CR2 = 0` (CCPC=0). All CCER and CCMR writes take effect immediately,
which is correct for polling-based commutation where we update registers every loop.

---

## 11. UART Command Interface

The board speaks two protocols on UART1 (115200, PB6 TX / PB4 RX):

### Binary frame (from ESP32)

```
[0xAB] [int8_t speed_pct  -100..+100] [checksum = 0xAB XOR speed_byte]
```

`speed > 0` → forward, `speed < 0` → reverse, `speed = 0` → coast stop.  
Duty is scaled: `duty = pct × 18` (so ±100 → ±1800 = full scale).

### ASCII commands (bench testing)

| Byte | Action |
|------|--------|
| `+`  | Forward at 33% duty (600/1800) |
| `-`  | Reverse at 33% duty |
| `0`  | Stop |
| `1`–`9` | Set duty to N×200 (200..1800) |
| `d`  | Dump hall state, duty, TIM1 registers, GPIO registers |
| `h`  | Print hall state 30 times at 50 ms intervals (spin wheel by hand) |
| `o`  | Open-loop: step through all 6 commutation positions ignoring hall |
| `t`  | Test: hold all 6 TIM1 outputs at 50% PWM simultaneously |
| `g`  | GPIO test: take PA8+PB14 away from TIM1 and drive HIGH as plain GPIO |

### Heartbeat

The board transmits once per second:
```
tick=<ms>  hall=<0-7>  csr=<hex UART status>\r\n
```

---

## 12. Motor Phase Order

If the motor jitters or spins in the wrong direction after flashing, swap motor phase wires.

**Method**: disconnect the motor connector, swap any two of the three phase wires,
reconnect, and test. If worse, try a different pair. There are only 3 permutations.

**Why asymmetry between forward and reverse is normal**: Hall sensors are not placed at
exactly 60° commutation boundaries. A slight offset means one direction gets natural
advance (early commutation, more torque) and the other gets retard. This is a property of
the physical motor, not a firmware bug. For basic forward/reverse control it can be
compensated by adjusting the duty for the weaker direction.

---

## 13. Reference Firmware

The reference firmware (`HoverboardOutputMM32SPIN05.hex`) was the key to confirming
register values. It was disassembled/compared by reading actual register state over UART
after letting it run.

Key confirmed values from the reference:
- `RCC_CR = 0x00105903` (bit 20 set = 72 MHz HSI enabled)
- `RCC_CFGR = 0x0A` (SW=10, SWS=10 → sysclk = 72 MHz HSI)
- `FMC_WS = 0x3A` (bits[2:0] = 2 wait states)
- UART: BRR=39, FRA=1 → 115200 @ 72 MHz
- Hall mapping convention: raw (non-inverted) 0=active
- Commutation: `apply_step` uses OCM=0x5 for grounded phase, CCPC=0

Source repository for alternate reference:
`https://github.com/EmanuelFeru/Hoverboard-Firmware-Hack-Gen2.x-MM32`  
File: `HoverBoardMindMotion/Src/bldc.c`

---

*Motor spinning as of 2026-04-27. Phase order confirmed by swapping wires.*

---

## 14. Slave Board Power Requirements

The slave board has **no onboard 12 V boost converter**. The 12 V gate driver
supply comes entirely from the master board.

### What must be connected for the slave to drive the motor

| Connection | Notes |
|---|---|
| 36 V battery → slave power input | Direct battery connection required |
| Inter-board UART cable (master ↔ slave) | Carries GND, TX, RX **and the 12 V gate driver rail** from the master |
| Power switch JST (or jumper short across its two pins) | Part of the 12 V enable path — without it the 12 V rail never comes up |
| Motor phase wires | Yellow / Blue / Green from heatsink side |
| Hall connector | VCC for halls also comes from master via inter-board cable |
| UART to ESP32 | PB6 (TX) → ESP32 RX, PB4 (RX) → ESP32 TX, 115200 8N1 |

### Why the power switch matters

The hoverboard power circuit works as follows:

1. Physical button press momentarily connects battery to the 12 V enable path.
2. MCU boots and immediately drives **PB2 HIGH** (power latch) — board stays on
   after button release.
3. With the switch JST connected (or shorted), the 12 V boost converter on the
   master board runs and the output is delivered to the slave via the inter-board
   cable.

On the master board in a complete hoverboard chassis the power button is always
wired. On the slave board in a standalone bench test the button JST is usually
disconnected — short its two pins together with a jumper wire to enable the
12 V rail.

### Symptom when 12 V is missing

- MCU boots, UART heartbeat prints correctly.
- `o` open-loop command produces **no motor twitch**.
- `g` GPIO test produces **no current draw change** on power supply.
- Power supply shows near-zero current draw from the slave.

### Motor phase wire order (confirmed)

Yellow → Blue → Green from the **heatsink side** of the motor connector.

---

## 15. Flash Unlock, Erase, and Program Procedure (MM32SPIN05PF)

Use this when the board is bricked or needs a full flash wipe.

> **Important:** Do NOT use `flash write_image erase` in OpenOCD.
> It fails with `"Cannot identify target as a STM32 family"` because the
> MM32SPIN05PF does not present a valid STM32 device ID.
> Use **pyOCD** for programming instead.

### Step 1 — Terminal 1: Start OpenOCD

```bash
openocd \
  -f interface/cmsis-dap.cfg \
  -f target/stm32f0x.cfg \
  -c "init; reset halt"
```

Leave this terminal running. OpenOCD listens on telnet port 4444.

### Step 2 — Terminal 2: Connect via telnet

```bash
nc localhost 4444
```

You get a `>` prompt.

### Step 3 — Confirm connection

```tcl
mdw 0xE000ED00 1
```

Expected: `0xe000ed00: 410cc200`  (Cortex-M0, MM32SPIN05PF confirmed)

### Step 4 — Unlock flash

```tcl
mww 0x40022004 0x45670123
mww 0x40022004 0xCDEF89AB
```

### Step 5 — Erase pages

Page size is **1 kB (0x400)**. Repeat this block for each page address:

```tcl
mww 0x40022010 0x00000002        ;# PER = 1 (page erase mode)
mww 0x40022014 <page_address>    ;# address of page to erase
mww 0x40022010 0x00000042        ;# PER = 1, STRT = 1 (start erase)
mdw 0x4002200C 1                 ;# poll SR — repeat until bit 0 (BSY) = 0
mww 0x4002200C 0x00000034        ;# clear EOP / ERR flags
```

Page addresses (32 kB total):
