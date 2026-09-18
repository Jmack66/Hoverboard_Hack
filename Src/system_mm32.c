/*
 * Minimal SystemInit for MM32SPIN05PF.
 *
 * The GD32 system_gd32f1x0.c tries to configure a 72 MHz PLL using the
 * GD32-specific RCU_CFG0 register layout. On MM32SPIN05PF the PLL lock
 * sequence is incompatible: writes to RCU_CFG0 (0x40021004) are ignored,
 * PLLRDY (bit 25 of 0x40021000) never sets, and the chip hangs before main().
 *
 * This file defines SystemInit, SystemCoreClock, and SystemCoreClockUpdate
 * as strong (non-weak) symbols. Because user object files are resolved before
 * framework archives, the linker will use these definitions and will not pull
 * in system_gd32f1x0.o at all — no multiple-definition conflict.
 *
 * MM32SPIN05PF clock:
 *   Reset default: CFGR_SW=00 → HSI/6 ≈ 12 MHz.
 *   Full speed:    set bit 20 of RCC_CR (HSI 72 MHz enable), then CFGR_SW=10.
 *   Confirmed by reference firmware: RCC_CR=0x00105903 (bit20 set),
 *                                    CFGR=0x0A (SW=10, SWS=10),
 *                                    FMC_WS=0x3A (2 wait states).
 *
 *   Our UART   = 72 000 000 / (39*16+1)    = 115 200 baud (exact, BRR=39 FRA=1)
 *   SysTick    = 72 000 000 / 1 000 - 1    = 71 999       (1 ms tick)
 *   PWM period = 72 000 000 / (2 × 20 000) = 1 800        (center-aligned, 20 kHz)
 */

#include <stdint.h>

#define RCC_CR   (*(volatile uint32_t*)0x40021000UL)
#define RCC_CFGR (*(volatile uint32_t*)0x40021004UL)
#define FMC_WS   (*(volatile uint32_t*)0x40022000UL)

uint32_t SystemCoreClock = 72000000UL;

void SystemInit(void)
{
    /* Set 2 flash wait states BEFORE boosting clock (required at 72 MHz). */
    FMC_WS = (FMC_WS & ~0x7U) | 2U;

    /* Enable HSI at 72 MHz (RCC_CR bit 20), then switch SYSCLK to it (SW=10). */
    RCC_CR   |= (1U << 20);
    RCC_CFGR  = (RCC_CFGR & ~0x3U) | 0x2U;
    while ((RCC_CFGR & 0xCU) != 0x8U);   /* wait until SWS=10 */
}

void SystemCoreClockUpdate(void)
{
    SystemCoreClock = 72000000UL;
}
