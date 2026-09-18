#!/usr/bin/env python3
"""
Direct flash programmer for MM32SPIN05PF.
Bypasses the pyocd FLM (which requires a running CPU) by writing
16-bit halfwords directly via the debug interface after manually
unlocking the flash controller.
"""

import struct
import sys
import time
from pyocd.core.helpers import ConnectHelper
from pyocd.core.target import Target

FIRMWARE = "/private/tmp/hoverboard-motor-gd32k6/.pio/build/K6_motor/firmware.bin"
FLASH_BASE  = 0x08000000
FLASH_KEYR  = 0x40022004
FLASH_OPTKEYR = 0x40022008
FLASH_SR    = 0x4002200C
FLASH_CR    = 0x40022010
KEY1 = 0x45670123
KEY2 = 0xCDEF89AB

def wait_not_busy(target, timeout=1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        sr = target.read_memory(FLASH_SR)
        if not (sr & 0x01):  # BUSY cleared
            return sr
    raise TimeoutError(f"Flash busy timeout, SR=0x{target.read_memory(FLASH_SR):08X}")

with ConnectHelper.session_with_chosen_probe(
    target_override="mm32spin05pf",
    pack=("/tmp/mm32_dfp_extract/1.0.8.zip",),
    connect_mode="halt",
) as session:
    target = session.target
    target.halt()

    print(f"  Target state: {target.get_state()}")

    # Unlock flash controller
    target.write_memory(FLASH_KEYR, KEY1)
    target.write_memory(FLASH_KEYR, KEY2)
    cr = target.read_memory(FLASH_CR)
    print(f"  FLASH_CR after unlock: 0x{cr:08X}  (LOCK bit should be 0: {'OK' if not (cr & 0x80) else 'STILL LOCKED'})")

    # Mass erase first (chip is blank but pyocd re-erases — do it ourselves)
    wait_not_busy(target)
    target.write_memory(FLASH_CR, 0x00000004)  # MER
    target.write_memory(FLASH_CR, 0x00000044)  # MER | STRT
    sr = wait_not_busy(target, timeout=10.0)
    print(f"  Mass erase done, SR=0x{sr:08X}")
    target.write_memory(FLASH_CR, 0x00000000)  # clear MER

    # Read firmware
    with open(FIRMWARE, "rb") as f:
        data = bytearray(f.read())
    if len(data) % 2:
        data.append(0xFF)
    print(f"  Programming {len(data)} bytes ({len(data)//2} halfwords)...")

    # Enable programming mode
    target.write_memory(FLASH_CR, 0x00000001)  # PG

    addr = FLASH_BASE
    for i in range(0, len(data), 2):
        hw = struct.unpack_from("<H", data, i)[0]
        target.write_memory_block8(addr, [data[i], data[i+1]])
        wait_not_busy(target)
        # Clear EOP
        target.write_memory(FLASH_SR, 0x00000020)
        addr += 2
        if i % 256 == 0:
            print(f"    {i}/{len(data)} bytes", end="\r", flush=True)

    print(f"\n  Programming complete.")

    # Lock flash
    target.write_memory(FLASH_CR, 0x00000080)  # LOCK

    # Verify first 8 bytes
    print("  Verifying first 8 bytes...")
    read_back = target.read_memory_block8(FLASH_BASE, 8)
    expected  = list(data[:8])
    if read_back == expected:
        print(f"  Verify OK: {[hex(b) for b in read_back]}")
    else:
        print(f"  MISMATCH: got {[hex(b) for b in read_back]}, expected {[hex(b) for b in expected]}")

    target.reset()
    print("  Reset. Done.")
