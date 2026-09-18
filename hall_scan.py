"""
Scan GPIOA/B/C IDR for toggling bits while the motor is spun by hand.
Run: pyocd commander -t mm32spin05pf --pack /tmp/mm32_dfp_extract/1.0.8.zip -s /tmp/hall_scan.py
"""
import time

GPIOA_IDR = 0x48000008
GPIOB_IDR = 0x48000408
GPIOC_IDR = 0x48000808

samples = 200
interval = 0.05  # seconds

print("Sampling GPIO IDR for toggling bits...")
print("SPIN THE MOTOR NOW")
print()

min_a = max_a = target.read32(GPIOA_IDR)
min_b = max_b = target.read32(GPIOB_IDR)
min_c = max_c = target.read32(GPIOC_IDR)

for i in range(samples):
    a = target.read32(GPIOA_IDR)
    b = target.read32(GPIOB_IDR)
    c = target.read32(GPIOC_IDR)
    min_a &= a; max_a |= a
    min_b &= b; max_b |= b
    min_c &= c; max_c |= c
    time.sleep(interval)

toggle_a = min_a ^ max_a
toggle_b = min_b ^ max_b
toggle_c = min_c ^ max_c

print(f"GPIOA toggling bits: 0x{toggle_a:04X}  = {[i for i in range(16) if (toggle_a>>i)&1]}")
print(f"GPIOB toggling bits: 0x{toggle_b:04X}  = {[i for i in range(16) if (toggle_b>>i)&1]}")
print(f"GPIOC toggling bits: 0x{toggle_c:04X}  = {[i for i in range(16) if (toggle_c>>i)&1]}")
print()
print(f"GPIOA final IDR: 0x{a:04X}")
print(f"GPIOB final IDR: 0x{b:04X}")
print(f"GPIOC final IDR: 0x{c:04X}")
