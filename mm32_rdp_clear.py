#!/usr/bin/env python3
"""
MM32SPIN05PF: Phase 1 — clear RDP via SRAM stub.

RDP (read protection) blocks all flash writes through the debug port — that's
why the previous run showed 1568 PGERR errors and reads returned zeros.

This script ONLY clears RDP. After it succeeds, the chip auto-mass-erases on
reset. Then run mm32_flash.py to program firmware on the clean chip.

Halt strategy: DEMCR.VC_CORERESET + AIRCR.SYSRESETREQ
  - VC_CORERESET tells the CPU to halt at the very first instruction after reset
  - SYSRESETREQ triggers a soft reset WITHOUT resetting the debug domain
    (DEMCR survives), so the vector catch fires on the next boot
  - This breaks a tight IWDG reset loop without needing NRST control
"""
import struct, time
from pyocd.core.helpers import ConnectHelper

FLASH_KEYR    = 0x40022004
FLASH_OPTKEYR = 0x40022008
FLASH_SR      = 0x4002200C
FLASH_CR      = 0x40022010
OB_RDP        = 0x1FFFF800
IWDG_KR       = 0x40003000
IWDG_PR       = 0x40003004
IWDG_RLR      = 0x40003008

DHCSR         = 0xE000EDF0
DEMCR         = 0xE000EDFC
AIRCR         = 0xE000ED0C

KEY1 = 0x45670123
KEY2 = 0xCDEF89AB

SRAM_BASE = 0x20000000
SRAM_SP   = 0x20000FF0
FLAG_ADDR = 0x20000200
FLAG_VAL  = 0xCAFEBABE
STUB_BIN  = "/tmp/stub.bin"

def wm(t, a, v): t.write_memory(a, v)
def rm(t, a):    return t.read_memory(a)

def halt_via_vector_catch(t):
    """Halt CPU by setting VC_CORERESET then issuing SYSRESETREQ."""
    # Write via memory (works even if core is in non-halted/reset state)
    wm(t, DHCSR, 0xA05F0003)   # DBGKEY | C_DEBUGEN | C_HALT
    wm(t, DEMCR, 0x00000001)   # VC_CORERESET
    wm(t, AIRCR, 0x05FA0004)   # VECTKEY | SYSRESETREQ
    time.sleep(0.15)            # wait for reset + halt

    deadline = time.time() + 2.0
    while time.time() < deadline:
        dhcsr = rm(t, DHCSR)
        if dhcsr & (1 << 17):  # S_HALT
            return dhcsr
        time.sleep(0.02)
    raise RuntimeError(f"Core not halted after vector catch, DHCSR=0x{rm(t, DHCSR):08X}")

def extend_iwdg(t):
    wm(t, IWDG_KR,  0x5555)   # unlock PR/RLR
    wm(t, IWDG_PR,  6)        # /256
    wm(t, IWDG_RLR, 0x0FFF)   # max reload (~26 s at 40 kHz)
    wm(t, IWDG_KR,  0xAAAA)   # reload now

def wait_busy(t, label="", timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        wm(t, IWDG_KR, 0xAAAA)
        sr = rm(t, FLASH_SR)
        if not (sr & 1):
            return sr
        time.sleep(0.002)
    raise TimeoutError(f"busy at '{label}', SR=0x{rm(t, FLASH_SR):08X}")

with ConnectHelper.session_with_chosen_probe(
    target_override="mm32spin05pf",
    pack=("/tmp/mm32_dfp_extract/1.0.8.zip",),
    connect_mode="attach",      # don't reset on connect — just attach
) as session:
    t = session.target

    # ── Step 1: get core halted ───────────────────────────────────────
    t.halt()
    time.sleep(0.05)
    dhcsr = rm(t, DHCSR)
    print(f"DHCSR on entry: 0x{dhcsr:08X}  S_HALT={'yes' if dhcsr & (1<<17) else 'no'}")

    if not (dhcsr & (1 << 17)):
        print("Core not halted — using VC_CORERESET + SYSRESETREQ...")
        dhcsr = halt_via_vector_catch(t)
        print(f"Core halted, DHCSR=0x{dhcsr:08X}")
    else:
        print("Core already halted")

    # ── Step 2: immediately extend IWDG ──────────────────────────────
    wm(t, IWDG_KR, 0xAAAA)
    extend_iwdg(t)
    print("IWDG extended (~26 s)")

    # ── Step 3: load SRAM stub (unlocks FLASH_KEYR from CPU context) ─
    with open(STUB_BIN, "rb") as f:
        stub = list(f.read())
    t.write_memory_block8(SRAM_BASE, stub)
    print(f"Stub loaded: {len(stub)} bytes at 0x{SRAM_BASE:08X}")
    wm(t, FLAG_ADDR, 0x00000000)

    # ── Step 4: jump to stub ─────────────────────────────────────────
    t.write_core_register("sp",   SRAM_SP)
    t.write_core_register("pc",   SRAM_BASE | 1)  # Thumb
    t.write_core_register("xpsr", 0x01000000)
    print(f"Running stub...")
    t.resume()

    # ── Step 5: wait for stub to set flag ────────────────────────────
    deadline = time.time() + 6.0
    while time.time() < deadline:
        flag = rm(t, FLAG_ADDR)
        if flag == FLAG_VAL:
            break
        time.sleep(0.05)
    else:
        print(f"WARNING: stub flag not set (got 0x{rm(t, FLAG_ADDR):08X}), continuing anyway")

    t.halt()
    cr = rm(t, FLASH_CR)
    print(f"FLASH_CR after stub: 0x{cr:08X}  LOCK={'SET' if cr & 0x80 else 'CLR'}")

    if cr & 0x80:
        raise RuntimeError("Flash still locked after stub — stub may not have run correctly")

    # ── Step 6: clear RDP option byte ────────────────────────────────
    # Unlock option byte programming (same keys as KEYR on MM32)
    wm(t, FLASH_OPTKEYR, KEY1)
    wm(t, FLASH_OPTKEYR, KEY2)
    time.sleep(0.02)
    cr = rm(t, FLASH_CR)
    print(f"FLASH_CR after OPTKEYR: 0x{cr:08X}  OPTWRE={'set' if cr & 0x200 else 'CLR'}")

    # Erase option bytes (use sleep not polling to avoid WAIT ACK crash)
    wait_busy(t, "pre-opter")
    wm(t, FLASH_CR, 0x00000620)   # OPTER | STRT | OPTWRE
    print("Option byte erase started, sleeping 500 ms...")
    time.sleep(0.5)
    wm(t, FLASH_SR, 0x20)         # clear EOP

    # Write RDP = 0xA5 (no protection), complement 0x5A
    wm(t, FLASH_CR, 0x00000210)   # OPTPG | OPTWRE
    t.write_memory_block8(OB_RDP, [0xA5, 0x5A])
    time.sleep(0.1)
    sr = rm(t, FLASH_SR)
    print(f"After RDP write: SR=0x{sr:08X}  EOP={'set' if sr & 0x20 else 'CLR'}")

    ob = rm(t, OB_RDP)
    print(f"OB_RDP readback: 0x{ob:08X}  (expect 0x5AA5 if RDP cleared)")

    wm(t, FLASH_CR, 0x00000080)   # LOCK

    # ── Step 7: reset — chip auto-mass-erases to remove RDP ──────────
    print("Resetting — chip will auto-erase flash to remove RDP...")
    t.reset()
    print("Done. Wait ~2 s for auto-erase, then run mm32_flash.py")
