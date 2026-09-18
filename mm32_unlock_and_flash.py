#!/usr/bin/env python3
"""
MM32SPIN05PF: clear RDP and flash firmware.

Sequence mirrors the FLM's Init → EraseChip → ProgramPage:
  1. Immediately feed+extend IWDG (hardware WDT at 0x40003000)
  2. Unlock FLASH_KEYR and FLASH_OPTKEYR
  3. Erase option bytes at 0x1FFFF800
  4. Write RDP=0xA5 (no protection)
  5. Mass-erase main flash
  6. Program firmware halfword-by-halfword
  7. Reset
"""
import struct, time
from pyocd.core.helpers import ConnectHelper

FIRMWARE    = "/private/tmp/hoverboard-motor-gd32k6/.pio/build/K6_motor/firmware.bin"
FLASH_BASE  = 0x08000000
FLASH_KEYR  = 0x40022004
FLASH_OPTKEYR = 0x40022008
FLASH_SR    = 0x4002200C
FLASH_CR    = 0x40022010
FLASH_OBR   = 0x4002201C
OB_RDP      = 0x1FFFF800
IWDG_KR     = 0x40003000
IWDG_PR     = 0x40003004
IWDG_RLR    = 0x40003008

KEY1    = 0x45670123
KEY2    = 0xCDEF89AB

def wm(t, addr, val):
    t.write_memory(addr, val)

def rm(t, addr):
    return t.read_memory(addr)

def feed_wdt(t):
    wm(t, IWDG_KR, 0xAAAA)

def extend_wdt(t):
    """Set IWDG to maximum timeout (~26 s at 40 kHz LSI)."""
    wm(t, IWDG_KR, 0x5555)   # unlock PR/RLR
    wm(t, IWDG_PR,  6)        # prescaler /256
    wm(t, IWDG_RLR, 0x0FFF)  # reload max
    wm(t, IWDG_KR, 0xAAAA)   # reload now

def wait_not_busy(t, label="", timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        feed_wdt(t)
        sr = rm(t, FLASH_SR)
        if not (sr & 0x01):
            return sr
    raise TimeoutError(f"Busy timeout at '{label}', SR=0x{rm(t, FLASH_SR):08X}")

def unlock_flash(t):
    wm(t, FLASH_KEYR, KEY1)
    wm(t, FLASH_KEYR, KEY2)

def unlock_optbytes(t):
    wm(t, FLASH_OPTKEYR, KEY1)
    wm(t, FLASH_OPTKEYR, KEY2)

with ConnectHelper.session_with_chosen_probe(
    target_override="mm32spin05pf",
    pack=("/tmp/mm32_dfp_extract/1.0.8.zip",),
    connect_mode="halt",
) as session:
    t = session.target
    t.halt()

    # ── Step 1: feed + extend IWDG immediately ──────────────────────
    feed_wdt(t)
    extend_wdt(t)
    print("IWDG extended to ~26 s timeout")

    # ── Step 2: unlock flash and option bytes ───────────────────────
    unlock_flash(t)
    unlock_optbytes(t)
    cr = rm(t, FLASH_CR)
    print(f"FLASH_CR after unlock: 0x{cr:08X}  LOCK={'SET' if cr & 0x80 else 'CLR'}")

    # ── Step 3: erase option bytes (clears RDP) ─────────────────────
    wait_not_busy(t, "pre-opter")
    # Write OPTKEYR again (FLM does this inside FLASH_EraseOptionBlock)
    wm(t, FLASH_OPTKEYR, KEY1)
    wm(t, FLASH_OPTKEYR, KEY2)
    wm(t, FLASH_CR, 0x00000020)   # OPTER
    wm(t, FLASH_CR, 0x00000060)   # OPTER | STRT
    sr = wait_not_busy(t, "opter")
    wm(t, FLASH_SR, sr & 0x3C)    # clear error/EOP bits
    print(f"Option byte erase done, SR=0x{sr:08X}")

    # ── Step 4: write RDP = 0xA5 (no protection) ───────────────────
    # CR: clear OPTER, set OPTPG
    cr = rm(t, FLASH_CR)
    cr = (cr & ~0x20) | 0x10      # clear OPTER, set OPTPG
    wm(t, FLASH_CR, cr)
    t.write_memory_block8(OB_RDP, [0xA5, 0x5A])  # RDP=0xA5, ~RDP=0x5A
    sr = wait_not_busy(t, "optpg")
    wm(t, FLASH_SR, sr & 0x3C)
    print(f"RDP byte written, SR=0x{sr:08X}")

    # ── Step 5: mass-erase main flash ───────────────────────────────
    wm(t, FLASH_CR, 0x00000004)   # MER
    wm(t, FLASH_CR, 0x00000044)   # MER | STRT
    sr = wait_not_busy(t, "mer", timeout=20.0)
    wm(t, FLASH_SR, sr & 0x3C)
    print(f"Mass erase done, SR=0x{sr:08X}")

    # ── Step 6: program firmware ────────────────────────────────────
    with open(FIRMWARE, "rb") as f:
        data = bytearray(f.read())
    if len(data) % 2:
        data.append(0xFF)

    print(f"Programming {len(data)} bytes...")
    wm(t, FLASH_CR, 0x00000001)   # PG

    addr = FLASH_BASE
    for i in range(0, len(data), 2):
        feed_wdt(t)
        t.write_memory_block8(addr, [data[i], data[i+1]])
        wait_not_busy(t, f"pg@{addr:08X}")
        wm(t, FLASH_SR, 0x20)
        addr += 2
        if (i // 2) % 100 == 0:
            print(f"  {i}/{len(data)} bytes", end="\r", flush=True)

    wm(t, FLASH_CR, 0x00000080)   # LOCK
    print(f"\nProgramming complete.")

    # ── Verify first 16 bytes ────────────────────────────────────────
    rb = t.read_memory_block8(FLASH_BASE, 16)
    ex = list(data[:16])
    ok = rb == ex
    print(f"Verify: {'PASS' if ok else 'FAIL'}")
    print(f"  Got:      {[hex(b) for b in rb]}")
    if not ok:
        print(f"  Expected: {[hex(b) for b in ex]}")

    t.reset()
    print("Reset. Done.")
