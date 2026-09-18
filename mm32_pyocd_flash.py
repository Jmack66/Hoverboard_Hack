#!/usr/bin/env python3
"""
MM32SPIN05PF: flash via pyocd's built-in FlashLoader (uses FLM on CPU).
The FLM algorithm runs as Thumb code from SRAM, uses real STRH instructions.
This avoids the MEM-AP byte-lane issue that corrupts every second halfword.
"""
import time
from pyocd.core.helpers import ConnectHelper
from pyocd.flash.loader import FlashLoader

FIRMWARE   = "/private/tmp/hoverboard-motor-gd32k6/.pio/build/K6_motor/firmware.bin"
FLASH_BASE = 0x08000000
FLASH_OBR  = 0x4002201C   # Option byte register — RDPRT bit1
FLASH_WRP  = 0x40022020   # Write protection register
OB_RDP     = 0x1FFFF800
DHCSR      = 0xE000EDF0
DEMCR      = 0xE000EDFC
AIRCR      = 0xE000ED0C

def wm(t, a, v): t.write_memory(a, v)
def rm(t, a):    return t.read_memory(a)

def halt_cpu(t):
    """Halt via direct DHCSR write, fall back to vector-catch reset if needed."""
    t.halt()
    time.sleep(0.1)
    dhcsr = rm(t, DHCSR)
    if dhcsr & (1 << 17):
        print(f"Halted OK (DHCSR=0x{dhcsr:08X})")
        return
    print("Direct halt failed — using VC_CORERESET + SYSRESETREQ...")
    wm(t, DHCSR, 0xA05F0003)
    wm(t, DEMCR, 0x00000001)
    wm(t, AIRCR, 0x05FA0004)
    time.sleep(0.2)
    deadline = time.time() + 3.0
    while time.time() < deadline:
        dhcsr = rm(t, DHCSR)
        if dhcsr & (1 << 17):
            print(f"Halted via vector catch (DHCSR=0x{dhcsr:08X})")
            return
        time.sleep(0.02)
    raise RuntimeError(f"Cannot halt CPU, DHCSR=0x{rm(t, DHCSR):08X}")

with ConnectHelper.session_with_chosen_probe(
    target_override="mm32spin05pf",
    pack=("/tmp/mm32_dfp_extract/1.0.8.zip",),
    connect_mode="attach",
) as session:
    t = session.target
    halt_cpu(t)

    # Check actual RDP/WRP status before attempting flash
    obr = rm(t, FLASH_OBR)
    wrp = rm(t, FLASH_WRP)
    ob_rdp = rm(t, OB_RDP)
    print(f"FLASH_OBR = 0x{obr:08X}  RDPRT={'ACTIVE (RDP not cleared!)' if obr & 2 else 'off'}")
    print(f"FLASH_WRP = 0x{wrp:08X}  (all 1s = no page protection)")
    print(f"OB_RDP    = 0x{ob_rdp:08X}  (0xA5?? = unprotected, 0x5AA5 expected)")

    if obr & 2:
        raise RuntimeError("RDP is still active — run mm32_rdp_clear.py first, then wait for auto-erase")

    with open(FIRMWARE, "rb") as f:
        fw = f.read()

    print(f"Programming {len(fw)} bytes via FlashLoader (FLM, chip_erase, no smart_flash)...")
    loader = FlashLoader(session,
                         progress=lambda n: print(f"  {n:.0%}", end="\r", flush=True),
                         chip_erase="chip",
                         smart_flash=False)
    loader.add_data(FLASH_BASE, fw)
    loader.commit()
    print("\nFlashLoader done.")

    # Verify first 16 bytes
    rb = t.read_memory_block8(FLASH_BASE, 16)
    ex = list(fw[:16])
    ok = rb == ex
    print(f"Verify: {'PASS' if ok else 'FAIL'}")
    print(f"  Got:      {[hex(b) for b in rb]}")
    if not ok:
        print(f"  Expected: {[hex(b) for b in ex]}")

    t.reset()
    print("Reset. Done.")
