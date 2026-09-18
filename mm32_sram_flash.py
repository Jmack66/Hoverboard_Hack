#!/usr/bin/env python3
"""
MM32SPIN05PF: flash unlock via SRAM stub + direct halfword programming.

The FLASH_KEYR unlock fails via DAP writes when protection is active.
Running the same key sequence as native CPU code always works (this is how
the FLM works). Strategy:
  1. Load a tiny Thumb stub into SRAM
  2. Jump CPU to it (feeds IWDG + writes KEYR from CPU context)
  3. Wait for stub's done-flag in SRAM
  4. Halt CPU, then program flash via DAP (LOCK should now be 0)
"""
import struct, time
from pyocd.core.helpers import ConnectHelper
from pyocd.core.target import Target

FIRMWARE    = "/private/tmp/hoverboard-motor-gd32k6/.pio/build/K6_motor/firmware.bin"
FLASH_BASE  = 0x08000000
FLASH_SR    = 0x4002200C
FLASH_CR    = 0x40022010
SRAM_BASE   = 0x20000000
SRAM_SP     = 0x20000FF0
FLAG_ADDR   = 0x20000100
FLAG_VAL    = 0xCAFEBABE

# stub.bin compiled from stub.c (feeds IWDG, unlocks FLASH_KEYR, writes flag)
STUB_BIN = "/tmp/stub.bin"

def wm(t, a, v): t.write_memory(a, v)
def rm(t, a):    return t.read_memory(a)
def wm8(t, a, b): t.write_memory_block8(a, b)

def wait_busy(t, label="", timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        sr = rm(t, FLASH_SR)
        if not (sr & 1):
            return sr
        time.sleep(0.001)
    raise TimeoutError(f"busy at '{label}'")

with ConnectHelper.session_with_chosen_probe(
    target_override="mm32spin05pf",
    pack=("/tmp/mm32_dfp_extract/1.0.8.zip",),
    connect_mode="under_reset",
) as session:
    t = session.target
    # Release reset and halt atomically — under_reset leaves core in reset
    # state (not debug-halt), so register writes would fail without this.
    t.reset_and_halt()
    # Verify halt before proceeding
    state = t.get_state()
    if state != Target.State.HALTED:
        raise RuntimeError(f"Core not halted after reset_and_halt (state={state})")
    print(f"Core halted OK")

    # ── Feed IWDG immediately via DAP (buys us time) ─────────────────
    wm(t, 0x40003000, 0xAAAA)  # reload counter
    # Extend IWDG to maximum timeout before any slow operations
    wm(t, 0x40003000, 0x5555)   # unlock PR/RLR
    wm(t, 0x40003004, 6)        # prescaler /256
    wm(t, 0x40003008, 0x0FFF)  # max reload (~26 s)
    wm(t, 0x40003000, 0xAAAA)  # reload now

    # ── Load stub into SRAM ──────────────────────────────────────────
    with open(STUB_BIN, "rb") as f:
        stub = list(f.read())
    t.write_memory_block8(SRAM_BASE, stub)
    print(f"Stub loaded: {len(stub)} bytes at 0x{SRAM_BASE:08X}")

    # Clear flag location
    wm(t, FLAG_ADDR, 0x00000000)

    # ── Set PC = SRAM_BASE+1 (Thumb), SP, then resume ────────────────
    t.write_core_register("sp", SRAM_SP)
    t.write_core_register("pc", SRAM_BASE | 1)  # Thumb entry
    t.write_core_register("xpsr", 0x01000000)    # Thumb mode
    print(f"Starting stub at 0x{SRAM_BASE:08X}...")
    t.resume()

    # ── Wait for done flag ────────────────────────────────────────────
    deadline = time.time() + 5.0
    while time.time() < deadline:
        flag = rm(t, FLAG_ADDR)
        if flag == FLAG_VAL:
            break
        time.sleep(0.05)
    else:
        print(f"WARNING: stub flag not set (got 0x{rm(t, FLAG_ADDR):08X}), proceeding anyway")

    # ── Halt and check FLASH_CR ───────────────────────────────────────
    t.halt()
    cr = rm(t, FLASH_CR)
    print(f"FLASH_CR after stub: 0x{cr:08X}  LOCK={'SET' if cr & 0x80 else 'CLR (unlocked!)'}")

    if cr & 0x80:
        print("Flash still locked — check stub execution")
    else:
        # ── Mass erase ───────────────────────────────────────────────
        wm(t, FLASH_CR, 0x00000004)   # MER
        wm(t, FLASH_CR, 0x00000044)   # MER | STRT
        sr = wait_busy(t, "mer", 20.0)
        wm(t, FLASH_SR, sr & 0x3C)
        print(f"Mass erase done, SR=0x{sr:08X}")

        # ── Program firmware ─────────────────────────────────────────
        with open(FIRMWARE, "rb") as f:
            fw = bytearray(f.read())
        if len(fw) % 2:
            fw.append(0xFF)

        print(f"Programming {len(fw)} bytes ({len(fw)//2} halfwords)...")
        wm(t, FLASH_CR, 0x00000001)  # PG

        addr = FLASH_BASE
        errors = 0
        for i in range(0, len(fw), 2):
            # Feed IWDG periodically via DAP (stub is halted, so we feed it)
            if (i // 2) % 50 == 0:
                wm(t, 0x40003000, 0xAAAA)
            wm8(t, addr, [fw[i], fw[i+1]])
            sr = wait_busy(t, f"pg@{addr:08X}")
            if sr & 0x14:
                print(f"  PGERR/WRPRTERR at 0x{addr:08X}, SR=0x{sr:08X}")
                errors += 1
            wm(t, FLASH_SR, 0x20)
            addr += 2
            if (i // 2) % 200 == 0:
                print(f"  {i}/{len(fw)}", end="\r", flush=True)

        wm(t, FLASH_CR, 0x00000080)  # LOCK
        print(f"\nProgramming complete ({errors} errors)")

        # ── Verify ───────────────────────────────────────────────────
        # Resume stub briefly to feed IWDG before verify reads
        wm(t, 0x40003000, 0xAAAA)
        rb = t.read_memory_block8(FLASH_BASE, 16)
        ex = list(fw[:16])
        ok = rb == ex
        print(f"Verify: {'PASS' if ok else 'FAIL'}")
        print(f"  Got:      {[hex(b) for b in rb]}")
        if not ok:
            print(f"  Expected: {[hex(b) for b in ex]}")

    t.reset()
    print("Done.")
