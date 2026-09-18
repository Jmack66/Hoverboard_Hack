import time
import pyocd
from pyocd.core.helpers import ConnectHelper

GPIOA_IDR = 0x48000008
GPIOB_IDR = 0x48000408
GPIOC_IDR = 0x48000808

with ConnectHelper.session_with_chosen_probe(
        target_override="mm32spin05pf",
        pack="/tmp/mm32_dfp_extract/1.0.8.zip",
        connect_mode="attach") as session:
    target = session.target

    print("Sampling 200 times over 10 s — SPIN THE MOTOR NOW")
    a0 = target.read32(GPIOA_IDR)
    b0 = target.read32(GPIOB_IDR)
    c0 = target.read32(GPIOC_IDR)
    mn_a, mx_a = a0, a0
    mn_b, mx_b = b0, b0
    mn_c, mx_c = c0, c0

    for _ in range(200):
        a = target.read32(GPIOA_IDR)
        b = target.read32(GPIOB_IDR)
        c = target.read32(GPIOC_IDR)
        mn_a &= a; mx_a |= a
        mn_b &= b; mx_b |= b
        mn_c &= c; mx_c |= c
        time.sleep(0.05)

    tog_a = mn_a ^ mx_a
    tog_b = mn_b ^ mx_b
    tog_c = mn_c ^ mx_c
    print(f"GPIOA toggling bits: {[i for i in range(16) if (tog_a>>i)&1]}  (0x{tog_a:04X})")
    print(f"GPIOB toggling bits: {[i for i in range(16) if (tog_b>>i)&1]}  (0x{tog_b:04X})")
    print(f"GPIOC toggling bits: {[i for i in range(16) if (tog_c>>i)&1]}  (0x{tog_c:04X})")
    print(f"Final IDR  A=0x{a:04X}  B=0x{b:04X}  C=0x{c:04X}")
