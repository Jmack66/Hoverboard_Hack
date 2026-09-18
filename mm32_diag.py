#!/usr/bin/env python3
"""
MM32SPIN05PF: live diagnostic — halt CPU and dump key register state.
Run after flashing to verify firmware is running and UART is configured.
"""
import time
from pyocd.core.helpers import ConnectHelper

DHCSR      = 0xE000EDF0
DEMCR      = 0xE000EDFC
AIRCR      = 0xE000ED0C

# Flash/clock
FLASH_BASE = 0x08000000
RCC_CR     = 0x40021000
RCC_CFGR   = 0x40021004
FMC_WS     = 0x40022000

# UART1 (MM32 layout)
UART1_BASE = 0x40013800
UART1_GCR  = UART1_BASE + 0x18
UART1_CCR  = UART1_BASE + 0x1C
UART1_BRR  = UART1_BASE + 0x20
UART1_FRA  = UART1_BASE + 0x24
UART1_CSR  = UART1_BASE + 0x08

# GPIO B
GPIOB_BASE = 0x48000400
GPIOB_CRL  = GPIOB_BASE + 0x00
GPIOB_AFRL = GPIOB_BASE + 0x20

# RCU/AHB enables (APB2 includes USART1 clock)
RCU_AHBEN  = 0x40021014
RCU_APB2EN = 0x40021018

def wm(t, a, v): t.write_memory(a, v)
def rm(t, a):    return t.read_memory(a)

def halt_via_vector_catch(t):
    wm(t, DHCSR, 0xA05F0003)
    wm(t, DEMCR, 0x00000001)
    wm(t, AIRCR, 0x05FA0004)
    time.sleep(0.15)
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if rm(t, DHCSR) & (1 << 17):
            return
        time.sleep(0.02)
    raise RuntimeError(f"Core not halted, DHCSR=0x{rm(t, DHCSR):08X}")

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
        halt_via_vector_catch(t)
        dhcsr = rm(t, DHCSR)

    pc  = t.read_core_register("pc")
    sp  = t.read_core_register("sp")
    lr  = t.read_core_register("lr")

    print(f"\n── CPU state ──────────────────────────────")
    print(f"  DHCSR = 0x{dhcsr:08X}  S_HALT={'yes' if dhcsr&(1<<17) else 'no'}")
    print(f"  PC    = 0x{pc:08X}   (in {'flash' if 0x08000000<=pc<0x08080000 else 'SRAM' if 0x20000000<=pc<0x20010000 else 'system/fault'})")
    print(f"  SP    = 0x{sp:08X}")
    print(f"  LR    = 0x{lr:08X}")

    print(f"\n── Flash (first 16 bytes) ─────────────────")
    fb = t.read_memory_block8(FLASH_BASE, 16)
    print(f"  {[hex(b) for b in fb]}")

    print(f"\n── Clock / Flash wait ──────────────────────")
    rcc_cr   = rm(t, RCC_CR)
    rcc_cfgr = rm(t, RCC_CFGR)
    fmc_ws   = rm(t, FMC_WS)
    print(f"  RCC_CR   = 0x{rcc_cr:08X}  HSI72_EN={'yes' if rcc_cr&(1<<20) else 'NO'}")
    print(f"  RCC_CFGR = 0x{rcc_cfgr:08X}  SWS={( rcc_cfgr>>2)&3} ({'72MHz HSI' if ((rcc_cfgr>>2)&3)==2 else 'HSI/6=12MHz' if ((rcc_cfgr>>2)&3)==0 else '?'})")
    print(f"  FMC_WS   = 0x{fmc_ws:08X}  WS={(fmc_ws&7)}")

    print(f"\n── Peripheral clocks ───────────────────────")
    ahben  = rm(t, RCU_AHBEN)
    apb2en = rm(t, RCU_APB2EN)
    print(f"  AHBEN  = 0x{ahben:08X}  GPIOA={'on' if ahben&(1<<17) else 'OFF'}  GPIOB={'on' if ahben&(1<<18) else 'OFF'}  GPIOC={'on' if ahben&(1<<19) else 'OFF'}")
    print(f"  APB2EN = 0x{apb2en:08X}  TIM1={'on' if apb2en&(1<<11) else 'OFF'}  USART1={'on' if apb2en&(1<<14) else 'OFF'}")

    print(f"\n── UART1 registers ─────────────────────────")
    gcr = rm(t, UART1_GCR)
    ccr = rm(t, UART1_CCR)
    brr = rm(t, UART1_BRR)
    fra = rm(t, UART1_FRA)
    csr = rm(t, UART1_CSR)
    print(f"  GCR = 0x{gcr:08X}  EN={'yes' if gcr&1 else 'NO'}  TX={'on' if gcr&(1<<4) else 'OFF'}  RX={'on' if gcr&(1<<3) else 'OFF'}")
    print(f"  CCR = 0x{ccr:08X}  BRR={brr}  FRA={fra}")
    print(f"  CSR = 0x{csr:08X}")
    baud = 72000000 // (brr * 16 + fra) if brr else 0
    print(f"  Computed baud = {baud} (expect 115200)")

    print(f"\n── GPIOB UART pin config ───────────────────")
    crl  = rm(t, GPIOB_CRL)
    afrl = rm(t, GPIOB_AFRL)
    pb4  = (crl >> 16) & 0xF
    pb6  = (crl >> 24) & 0xF
    pb4af = (afrl >> 16) & 0xF
    pb6af = (afrl >> 24) & 0xF
    print(f"  CRL  = 0x{crl:08X}")
    print(f"  PB4 (RX): CRL nibble=0x{pb4:X} AF={pb4af}  (want nibble=8, AF=3)")
    print(f"  PB6 (TX): CRL nibble=0x{pb6:X} AF={pb6af}  (want nibble=B, AF=0)")
    print(f"  AFRL = 0x{afrl:08X}")

    print()
