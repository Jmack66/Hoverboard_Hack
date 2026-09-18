#!/usr/bin/env python3
"""
MM32SPIN05PF: Phase 2 — program firmware after RDP has been cleared.

Run this AFTER mm32_rdp_clear.py has reset the chip and the auto-erase
(~1-2 s) has completed. Flash should now be blank and unprotected.
"""
import struct, time
from pyocd.core.helpers import ConnectHelper

FIRMWARE   = "/private/tmp/hoverboard-motor-gd32k6/.pio/build/K6_motor/firmware.bin"
FLASH_BASE = 0x08000000
FLASH_KEYR = 0x40022004
FLASH_SR   = 0x4002200C
FLASH_CR   = 0x40022010
IWDG_KR    = 0x40003000
DHCSR      = 0xE000EDF0
DEMCR      = 0xE000EDFC
AIRCR      = 0xE000ED0C
KEY1 = 0x45670123
KEY2 = 0xCDEF89AB

def wm(t, a, v): t.write_memory(a, v)
def rm(t, a):    return t.read_memory(a)

def halt_via_vector_catch(t):
    wm(t, DHCSR, 0xA05F0003)
    wm(t, DEMCR, 0x00000001)
    wm(t, AIRCR, 0x05FA0004)
    time.sleep(0.15)
    deadline = time.time() + 2.0
    while time.time() < deadline:
        dhcsr = rm(t, DHCSR)
        if dhcsr & (1 << 17):
            return dhcsr
        time.sleep(0.02)
    raise RuntimeError(f"Core not halted, DHCSR=0x{rm(t, DHCSR):08X}")

def wait_busy(t, label="", timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        wm(t, IWDG_KR, 0xAAAA)
        sr = rm(t, FLASH_SR)
        if not (sr & 1):
            return sr
        time.sleep(0.002)
    raise TimeoutError(f"busy at '{label}'")

with ConnectHelper.session_with_chosen_probe(
    target_override="mm32spin05pf",
    pack=("/tmp/mm32_dfp_extract/1.0.8.zip",),
    connect_mode="attach",
) as session:
    t = session.target

    t.halt()
    time.sleep(0.05)
    dhcsr = rm(t, DHCSR)
    if not (dhcsr & (1 << 17)):
        print("Core not halted — using vector catch...")
        dhcsr = halt_via_vector_catch(t)
    print(f"Core halted, DHCSR=0x{dhcsr:08X}")

    wm(t, IWDG_KR, 0xAAAA)

    # Unlock flash
    wm(t, FLASH_KEYR, KEY1)
    wm(t, FLASH_KEYR, KEY2)
    time.sleep(0.02)
    cr = rm(t, FLASH_CR)
    print(f"FLASH_CR after unlock: 0x{cr:08X}  LOCK={'SET (failed!)' if cr & 0x80 else 'CLR'}")
    if cr & 0x80:
        raise RuntimeError("Flash unlock failed — RDP may still be active")

    # Mass erase
    wait_busy(t, "pre-mer")
    wm(t, FLASH_CR, 0x00000004)   # MER
    wm(t, FLASH_CR, 0x00000044)   # MER | STRT
    sr = wait_busy(t, "mer", 20.0)
    wm(t, FLASH_SR, sr & 0x3C)
    print(f"Mass erase done, SR=0x{sr:08X}")

    # Program firmware
    with open(FIRMWARE, "rb") as f:
        fw = bytearray(f.read())
    if len(fw) % 2:
        fw.append(0xFF)
    print(f"Programming {len(fw)} bytes ({len(fw)//2} halfwords)...")

    wm(t, FLASH_CR, 0x00000001)   # PG
    addr = FLASH_BASE
    errors = 0
    for i in range(0, len(fw), 2):
        if (i // 2) % 64 == 0:
            wm(t, IWDG_KR, 0xAAAA)
        hw = int.from_bytes(fw[i:i+2], 'little')
        t.write_memory(addr, hw, 16)   # 16-bit halfword write (CSW.SIZE=1)
        sr = wait_busy(t, f"pg@{addr:08X}")
        if sr & 0x14:
            print(f"  ERR at 0x{addr:08X} SR=0x{sr:08X}")
            errors += 1
        wm(t, FLASH_SR, 0x20)
        addr += 2
        if (i // 2) % 200 == 0:
            print(f"  {i}/{len(fw)}", end="\r", flush=True)

    wm(t, FLASH_CR, 0x00000080)   # LOCK
    print(f"\nProgramming done ({errors} errors)")

    # Verify first 16 bytes
    wm(t, IWDG_KR, 0xAAAA)
    rb = t.read_memory_block8(FLASH_BASE, 16)
    ex = list(fw[:16])
    ok = rb == ex
    print(f"Verify: {'PASS' if ok else 'FAIL'}")
    print(f"  Got:      {[hex(b) for b in rb]}")
    if not ok:
        print(f"  Expected: {[hex(b) for b in ex]}")

    t.reset()
    print("Reset. Done.")
