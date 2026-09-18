#!/usr/bin/env python3
"""
Clear RDP on MM32SPIN05PF using sleeps (not polling) to avoid WAIT ACK.
"""
import time
from pyocd.core.helpers import ConnectHelper

FLASH_KEYR    = 0x40022004
FLASH_OPTKEYR = 0x40022008
FLASH_SR      = 0x4002200C
FLASH_CR      = 0x40022010
OB_RDP_ADDR   = 0x1FFFF800

KEY1    = 0x45670123
KEY2    = 0xCDEF89AB
OPTKEY1 = 0x08192A3B
OPTKEY2 = 0x4C5D6E7F

with ConnectHelper.session_with_chosen_probe(
    target_override="mm32spin05pf",
    pack=("/tmp/mm32_dfp_extract/1.0.8.zip",),
    connect_mode="halt",
) as session:
    t = session.target
    t.halt()
    time.sleep(0.1)

    cr_before = t.read_memory(FLASH_CR)
    print(f"FLASH_CR on entry: 0x{cr_before:08X}  LOCK={'SET' if cr_before & 0x80 else 'CLR'}")

    # Unlock flash — write KEY1 then KEY2 with no gap
    t.write_memory(FLASH_KEYR, KEY1)
    t.write_memory(FLASH_KEYR, KEY2)
    time.sleep(0.05)

    cr = t.read_memory(FLASH_CR)
    print(f"FLASH_CR after KEYR: 0x{cr:08X}  LOCK={'SET (unlock failed!)' if cr & 0x80 else 'CLR (unlocked ok)'}")

    if cr & 0x80:
        print("Flash unlock failed — aborting.")
    else:
        # Unlock option bytes
        t.write_memory(FLASH_OPTKEYR, OPTKEY1)
        t.write_memory(FLASH_OPTKEYR, OPTKEY2)
        time.sleep(0.05)

        cr = t.read_memory(FLASH_CR)
        print(f"FLASH_CR after OPTKEYR: 0x{cr:08X}  OPTWRE={'SET' if cr & 0x200 else 'CLR'}")

        # Erase option bytes — use sleep, NOT polling, to avoid WAIT ACK
        t.write_memory(FLASH_CR, 0x00000620)  # OPTER | STRT | OPTWRE
        print("Option byte erase started, sleeping 500ms...")
        time.sleep(0.5)

        sr = t.read_memory(FLASH_SR)
        cr = t.read_memory(FLASH_CR)
        print(f"After erase: SR=0x{sr:08X}  CR=0x{cr:08X}")
        t.write_memory(FLASH_SR, 0x20)  # clear EOP

        # Program RDP = 0xA5 (no protection), complement 0x5A
        t.write_memory(FLASH_CR, 0x00000210)  # OPTPG | OPTWRE
        t.write_memory_block8(OB_RDP_ADDR, [0xA5, 0x5A])
        time.sleep(0.1)

        sr = t.read_memory(FLASH_SR)
        print(f"After RDP write: SR=0x{sr:08X}  EOP={'SET' if sr & 0x20 else 'CLR'}")

        ob_val = t.read_memory(OB_RDP_ADDR)
        print(f"OB_RDP raw read: 0x{ob_val:08X}  (expect 0x5AA5 if written, 0=still protected)")

        # Lock flash
        t.write_memory(FLASH_CR, 0x00000080)

        print("Resetting — chip should auto-mass-erase to remove RDP...")
        t.reset()
        print("Done. Now run mm32_direct_flash.py to flash firmware.")
