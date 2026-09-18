#!/usr/bin/env python3
"""Flash GD32F130K6 via OpenOCD telnet using 32-bit word DAP writes.

Bypasses two problems:
  1. stm32f1x driver can't probe GD32 (DBGMCU returns 0)
  2. picoprobe mwh byte-lane bug corrupts writes to word+2 addresses

Solution: use mww (32-bit word) writes directly to flash with PG=1.
GD32F130 supports word programming (GD32F1x0 UM §2.3.6 "32-bit word/16-bit
half word write at desired address by DBUS").  32-bit DAP writes are not
affected by the picoprobe byte-lane bug.
"""
import socket, sys, struct, re

HOST, PORT  = 'localhost', 4444
FLASH_BASE  = 0x08000000
PAGE_SIZE   = 0x400
FLASH_KEYR  = 0x40022004
FLASH_SR    = 0x4002200C
FLASH_CR    = 0x40022010
FLASH_AR    = 0x40022014
OB_BASE     = 0x1FFFF800   # option bytes: [0]=RDP/nRDP [2]=Data0 [6]=WRP0 [8]=WRP1


def recv(s, timeout=5.0):
    buf = b''
    s.settimeout(timeout)
    try:
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
            if b'> ' in buf:
                break
    except socket.timeout:
        pass
    return buf.decode(errors='replace')


def cmd(s, c, timeout=5.0):
    s.sendall((c + '\n').encode())
    return recv(s, timeout)


def mrw(s, addr):
    r = cmd(s, f'mrw 0x{addr:08x}')
    m = re.findall(r'0x([0-9a-fA-F]+)', r)
    return int(m[-1], 16) if m else 0


def wait_bsy(s, verbose=False):
    for _ in range(2000):
        sr = mrw(s, FLASH_SR)
        if not (sr & 0x01):   # BSY cleared
            if verbose:
                endf  = bool(sr & 0x20)
                pgerr = bool(sr & 0x04)
                wperr = bool(sr & 0x10)
                print(f'    SR=0x{sr:08x}  ENDF={int(endf)} PGERR={int(pgerr)} WPERR={int(wperr)}')
            cmd(s, f'mww 0x{FLASH_SR:08x} 0x00000034')
            if sr & 0x04:
                print('ERROR: PGERR — target address not erased')
                return False
            if sr & 0x10:
                print('ERROR: WPRTERR — page is write-protected')
                return False
            return True
    print('ERROR: BSY timeout')
    return False


def main():
    if len(sys.argv) < 2:
        print('Usage: python3 flash_gd32.py firmware.bin')
        sys.exit(1)

    fw = bytearray(open(sys.argv[1], 'rb').read())
    while len(fw) % 4:
        fw.append(0xFF)

    s = socket.socket()
    s.connect((HOST, PORT))
    recv(s)

    # ── Halt CPU (required before flash programming) ─────────────
    cmd(s, 'halt')

    # ── Read option bytes — RDP and WRP ──────────────────────────
    ob0 = mrw(s, OB_BASE)
    ob1 = mrw(s, OB_BASE + 0x04)
    ob2 = mrw(s, OB_BASE + 0x08)
    rdp  = ob0 & 0xFF
    wrp0 = ob2 & 0xFF
    wrp1 = (ob2 >> 16) & 0xFF
    print(f'Option bytes: OB[0]=0x{ob0:08x}  OB[1]=0x{ob1:08x}  OB[2]=0x{ob2:08x}')
    print(f'  RDP=0x{rdp:02x}  WRP0=0x{wrp0:02x}  WRP1=0x{wrp1:02x}')
    rdp_protected = (rdp != 0xA5)
    if rdp_protected:
        print(f'  ** RDP=0x{rdp:02x} — level-1 protection active.')
        print(f'     DAP flash reads return 0xFF. Writes may still work.')
        print(f'     Will skip verify and reset; check board behaviour instead.')

    # ── Unlock flash ────────────────────────────────────────────
    cmd(s, f'mww 0x{FLASH_KEYR:08x} 0x45670123')
    cmd(s, f'mww 0x{FLASH_KEYR:08x} 0xCDEF89AB')
    cr = mrw(s, FLASH_CR)
    print(f'FLASH_CR after unlock: 0x{cr:08x}')
    if cr & 0x80:
        print('ERROR: flash still locked.')
        s.close(); sys.exit(1)

    # ── Erase pages ─────────────────────────────────────────────
    n_pages = (len(fw) + PAGE_SIZE - 1) // PAGE_SIZE
    print(f'Firmware {len(fw)} B — erasing {n_pages} pages...')
    for p in range(n_pages):
        pa = FLASH_BASE + p * PAGE_SIZE
        cmd(s, f'mww 0x{FLASH_CR:08x} 0x00000002')
        cmd(s, f'mww 0x{FLASH_AR:08x} 0x{pa:08x}')
        cmd(s, f'mww 0x{FLASH_CR:08x} 0x00000042')
        if not wait_bsy(s): s.close(); sys.exit(1)
        v = mrw(s, pa)
        print(f'  page {p} @ 0x{pa:08x}: {"OK" if v == 0xFFFFFFFF else f"WARN 0x{v:08x}"}')

    # ── Test write: single word to confirm writes reach flash ────
    print('Diagnostic: test write to 0x08000000...')
    cmd(s, f'mww 0x{FLASH_CR:08x} 0x00000001')  # PG=1
    cr_check = mrw(s, FLASH_CR)
    print(f'  FLASH_CR with PG set: 0x{cr_check:08x}  (PG={cr_check & 1})')
    if not (cr_check & 1):
        print('ERROR: PG bit did not stick — flash may have re-locked.')
        s.close(); sys.exit(1)
    cmd(s, f'mww 0x{FLASH_BASE:08x} 0xDEADBEEF')
    sr_test = mrw(s, FLASH_SR)
    fl_test = mrw(s, FLASH_BASE)
    cmd(s, f'mww 0x{FLASH_SR:08x} 0x00000034')  # clear SR
    print(f'  FLASH_SR=0x{sr_test:08x}  flash[0x08000000]=0x{fl_test:08x}')
    endf  = bool(sr_test & 0x20)
    pgerr = bool(sr_test & 0x04)
    wperr = bool(sr_test & 0x10)
    if endf and fl_test == 0xDEADBEEF:
        print('  ENDF set + read-back correct → programming works fine.')
    elif endf:
        print('  ENDF set but read-back=0xFF → RDP blocking reads. Writes work.')
    elif pgerr:
        print('ERROR: PGERR — erase did not actually clear the page.')
        s.close(); sys.exit(1)
    elif wperr:
        print('ERROR: WPRTERR — page is write-protected (WRP in option bytes).')
        s.close(); sys.exit(1)
    else:
        # ── mww failed: try mwh (16-bit DAP halfword write) ──────
        print('  mww ENDF=0. Trying mwh (16-bit DAP)...')
        # re-erase page 0 first (mww may have dirtied it even without ENDF)
        cmd(s, f'mww 0x{FLASH_CR:08x} 0x00000002')
        cmd(s, f'mww 0x{FLASH_AR:08x} 0x{FLASH_BASE:08x}')
        cmd(s, f'mww 0x{FLASH_CR:08x} 0x00000042')
        if not wait_bsy(s): s.close(); sys.exit(1)

        cmd(s, f'mww 0x{FLASH_CR:08x} 0x00000001')  # PG=1
        cmd(s, f'mwh 0x{FLASH_BASE:08x} 0xABCD')
        sr_mwh = mrw(s, FLASH_SR)
        fl_mwh = mrw(s, FLASH_BASE)
        cmd(s, f'mww 0x{FLASH_SR:08x} 0x00000034')
        print(f'  mwh: SR=0x{sr_mwh:08x}  flash[0]=0x{fl_mwh:08x}')
        mwh_works = bool(sr_mwh & 0x20)

        # re-erase page 0 before CPU stub test
        cmd(s, f'mww 0x{FLASH_CR:08x} 0x00000002')
        cmd(s, f'mww 0x{FLASH_AR:08x} 0x{FLASH_BASE:08x}')
        cmd(s, f'mww 0x{FLASH_CR:08x} 0x00000042')
        if not wait_bsy(s): s.close(); sys.exit(1)

        # ── CPU STRH diagnostic stub ──────────────────────────────
        # Writes one halfword, waits BSY, saves SR to SRAM, BKPTs.
        # R0=flash_dest  R1=halfword_val  R4=FLASH_SR  R5=sram_save_addr
        # 0x00  8001  STRH R1,[R0]
        # 0x02  6823  LDR R3,[R4]      <- BSY_WAIT
        # 0x04  07DB  LSLS R3,R3,#31
        # 0x06  D4FC  BMI BSY_WAIT
        # 0x08  6823  LDR R3,[R4]      read final SR (ENDF should be set)
        # 0x0A  602B  STR R3,[R5]      save SR to SRAM
        # 0x0C  BE00  BKPT #0
        DIAG_STUB = [0x68238001, 0xD4FC07DB, 0x602B6823, 0x0000BE00]
        DIAG_ADDR = 0x20000000
        DIAG_SR   = 0x20000010   # where saved SR lands (after 4 stub words)
        STACK_TOP = 0x20001000

        print('  CPU STRH diagnostic stub...')
        cmd(s, f'mww 0x{FLASH_CR:08x} 0x00000001')  # PG=1
        for i, w in enumerate(DIAG_STUB):
            cmd(s, f'mww 0x{DIAG_ADDR + i*4:08x} 0x{w:08x}')
        cmd(s, 'halt')
        cmd(s, f'reg r0 0x{FLASH_BASE:08x}')
        cmd(s, f'reg r1 0xDEAD')
        cmd(s, f'reg r4 0x{FLASH_SR:08x}')
        cmd(s, f'reg r5 0x{DIAG_SR:08x}')
        cmd(s, f'reg sp 0x{STACK_TOP:08x}')
        cmd(s, 'reg xpsr 0x01000000')
        cmd(s, f'reg pc 0x{DIAG_ADDR:08x}')
        cmd(s, 'resume')
        r = cmd(s, 'wait_halt 5000', timeout=10.0)
        if 'timeout' in r.lower():
            print('  ERROR: diagnostic stub timed out (stuck in BSY loop?)')
            s.close(); sys.exit(1)
        saved_sr = mrw(s, DIAG_SR)
        fl_strh  = mrw(s, FLASH_BASE)
        cmd(s, f'mww 0x{FLASH_SR:08x} 0x00000034')
        strh_works = bool(saved_sr & 0x20)
        print(f'  STRH: saved_SR=0x{saved_sr:08x}  flash[0]=0x{fl_strh:08x}  ENDF={int(strh_works)}')

        if not mwh_works and not strh_works:
            print()
            print('FATAL: Neither DAP mwh nor CPU STRH triggered flash programming.')
            print('Possible causes:')
            print('  1. BOOT0 pin is HIGH → chip in bootloader mode, flash writes blocked')
            print('  2. Some GD32F130K6 silicon revision requires a different sequence')
            print('  3. Hardware issue (VDD too low for programming)')
            print()
            print('Fallback: use the UART bootloader instead of SWD:')
            print('  - Pull BOOT0 pin HIGH, reset chip')
            print('  - stm32flash -w firmware.bin -v -b 115200 /dev/ttyXXX')
            s.close(); sys.exit(1)

        # re-erase page 0 one last time before full programming
        cmd(s, f'mww 0x{FLASH_CR:08x} 0x00000002')
        cmd(s, f'mww 0x{FLASH_AR:08x} 0x{FLASH_BASE:08x}')
        cmd(s, f'mww 0x{FLASH_CR:08x} 0x00000042')
        if not wait_bsy(s): s.close(); sys.exit(1)

        if strh_works:
            print('CPU STRH works → using full CPU stub for programming.')
            use_stub = True
        else:
            print('mwh works → using mwh halfword DAP writes.')
            use_stub = False

    # endf=True means mww worked; re-erase p0 and use mww for full programming
    if endf:
        cmd(s, f'mww 0x{FLASH_CR:08x} 0x00000002')
        cmd(s, f'mww 0x{FLASH_AR:08x} 0x{FLASH_BASE:08x}')
        cmd(s, f'mww 0x{FLASH_CR:08x} 0x00000042')
        if not wait_bsy(s): s.close(); sys.exit(1)
        use_stub = False
        use_mww  = True
    else:
        use_mww = False

    # ── Full programming ─────────────────────────────────────────
    cmd(s, f'mww 0x{FLASH_CR:08x} 0x00000001')  # PG=1

    if use_mww:
        print(f'Programming {len(fw)} bytes via mww...')
        for i in range(0, len(fw), 4):
            w = struct.unpack_from('<I', fw, i)[0]
            cmd(s, f'mww 0x{FLASH_BASE + i:08x} 0x{w:08x}')
            if not wait_bsy(s): s.close(); sys.exit(1)
            if i % 512 == 0:
                print(f'  {i}/{len(fw)} B', end='\r', flush=True)
        print(f'  {len(fw)}/{len(fw)} B done.')
    elif use_stub:
        # CPU stub: LDRH/STRH loop in SRAM
        # R0=flash_dst  R1=sram_src  R2=halfword_count  R4=FLASH_SR
        # loop: LDRH R3,[R1] | STRH R3,[R0] | ADDS R1,#2 | ADDS R0,#2
        #       LDR R3,[R4]  | LSLS R3,#31  | BMI bsy_wait
        #       MOVS R3,#0x34 | STR R3,[R4] | SUBS R2,#1 | BNE loop | BKPT
        STUB = [0x8003880B, 0x30023102, 0x07DB6823, 0x2334D4FC,
                0x3A016023, 0xBE00D1F4]
        STUB_ADDR = 0x20000000
        DATA_ADDR = 0x20000020
        CHUNK     = (0x1000 - (DATA_ADDR - 0x20000000) - 512) & ~3  # 3552 B
        STACK_TOP = 0x20001000

        for i, w in enumerate(STUB):
            cmd(s, f'mww 0x{STUB_ADDR + i*4:08x} 0x{w:08x}')

        offset = 0
        while offset < len(fw):
            chunk = bytes(fw[offset: offset + CHUNK])
            while len(chunk) % 4:
                chunk += b'\xff'
            n_hw = len(chunk) // 2
            for i in range(0, len(chunk), 4):
                w = struct.unpack_from('<I', chunk, i)[0]
                cmd(s, f'mww 0x{DATA_ADDR + i:08x} 0x{w:08x}')
            flash_dst = FLASH_BASE + offset
            print(f'  stub: 0x{flash_dst:08x} + {len(chunk)} B...', end=' ', flush=True)
            cmd(s, 'halt')
            cmd(s, f'reg r0 0x{flash_dst:08x}')
            cmd(s, f'reg r1 0x{DATA_ADDR:08x}')
            cmd(s, f'reg r2 {n_hw}')
            cmd(s, f'reg r4 0x{FLASH_SR:08x}')
            cmd(s, f'reg sp 0x{STACK_TOP:08x}')
            cmd(s, 'reg xpsr 0x01000000')
            cmd(s, f'reg pc 0x{STUB_ADDR + 1:08x}')  # ODD address sets T-bit in hardware
            cmd(s, 'resume')
            r = cmd(s, 'wait_halt 30000', timeout=35.0)
            if 'timeout' in r.lower():
                print(f'ERROR: stub timeout'); s.close(); sys.exit(1)
            fl0 = mrw(s, flash_dst)
            print(f'flash[0]=0x{fl0:08x}')
            offset += len(chunk)
    else:
        # mwh halfword writes (word-aligned OK; word+2 has picoprobe byte-swap bug
        # but we can compensate: swap the two bytes before writing word+2 halfwords)
        print(f'Programming {len(fw)} bytes via mwh...')
        for i in range(0, len(fw), 2):
            hw_val = struct.unpack_from('<H', fw, i)[0]
            addr = FLASH_BASE + i
            if addr % 4 == 2:
                # picoprobe byte-lane bug: bytes are swapped for word+2 writes
                # compensate by pre-swapping
                hw_val = ((hw_val & 0xFF) << 8) | ((hw_val >> 8) & 0xFF)
            cmd(s, f'mwh 0x{addr:08x} 0x{hw_val:04x}')
            if i % 512 == 0:
                print(f'  {i}/{len(fw)} B', end='\r', flush=True)
        wait_bsy(s)
        print(f'  {len(fw)}/{len(fw)} B done.')

    # ── Lock ────────────────────────────────────────────────────
    cmd(s, f'mww 0x{FLASH_CR:08x} 0x00000080')

    # ── Verify (skip if RDP is blocking reads) ───────────────────
    if rdp_protected:
        print('\nRDP active — skipping read-back verify.')
        print('Resetting. Check board behaviour to confirm firmware ran.')
        cmd(s, 'reset run')
    else:
        print(f'\nVerifying {len(fw)} bytes...')
        ok, bad = True, 0
        for i in range(0, len(fw), 4):
            exp = struct.unpack_from('<I', fw, i)[0]
            got = mrw(s, FLASH_BASE + i)
            if got != exp:
                print(f'  ✗ 0x{FLASH_BASE+i:08x}: exp 0x{exp:08x}  got 0x{got:08x}')
                ok = False
                bad += 1
                if bad >= 10:
                    print('  (stopping after 10)')
                    break
        if ok:
            print('All verified OK. Resetting...')
            cmd(s, 'reset run')
        else:
            print(f'Verification FAILED ({bad} mismatches).')
            print('Resetting anyway — check board behaviour.')
            cmd(s, 'reset run')

    s.close()


if __name__ == '__main__':
    main()
