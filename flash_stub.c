/*
 * Flash programmer stub for MM32SPIN05PF.
 * Runs from SRAM at 0x20000000 (set PC = entry | 1 for Thumb).
 * Firmware data placed at SRAM+0x1000 by the host before execution.
 * Writes success/error flag to SRAM+0x0F00 when done.
 */
#include <stdint.h>

#define FLASH_KEYR  ((volatile uint32_t*)0x40022004)
#define FLASH_CR    ((volatile uint32_t*)0x40022010)
#define FLASH_SR    ((volatile uint32_t*)0x4002200C)
#define IWDG_KR     ((volatile uint32_t*)0x40003000)

/* Host writes firmware here, and its halfword count at FW_COUNT_ADDR. */
#define FW_DATA_BASE  0x20001000u
#define FW_COUNT_ADDR ((volatile uint32_t*)0x20000F00)   /* halfword count (not bytes) */
#define FLAG_ADDR     ((volatile uint32_t*)0x20000F04)   /* result: CAFEBABE or DEADBEEF */

#define FLASH_BASE    0x08000000u
#define KEY1          0x45670123u
#define KEY2          0xCDEF89ABu

__attribute__((noreturn, noinline, section(".entry")))
void entry(void)
{
    *IWDG_KR = 0xAAAAu;

    /* Unlock — CPU writes to KEYR always work (DAP writes don't on MM32) */
    *FLASH_KEYR = KEY1;
    *FLASH_KEYR = KEY2;

    if (*FLASH_CR & 0x80u) {
        *FLAG_ADDR = 0xDEADBEEFu;
        for(;;);
    }

    /* Mass erase */
    *FLASH_CR = 0x04u;    /* MER */
    *FLASH_CR = 0x44u;    /* MER | STRT */
    while (*FLASH_SR & 1u) { *IWDG_KR = 0xAAAAu; }
    *FLASH_SR = 0x3Cu;    /* clear EOP/errors */

    /* Program halfword by halfword */
    *FLASH_CR = 0x01u;    /* PG */

    uint32_t count = *FW_COUNT_ADDR;
    volatile uint16_t *src = (volatile uint16_t*)FW_DATA_BASE;
    volatile uint16_t *dst = (volatile uint16_t*)FLASH_BASE;

    for (uint32_t i = 0; i < count; i++) {
        *IWDG_KR = 0xAAAAu;
        *dst++ = *src++;
        while (*FLASH_SR & 1u) {}
        if (*FLASH_SR & 0x14u) {
            *FLAG_ADDR = 0xDEADBEEFu;
            for(;;);
        }
        *FLASH_SR = 0x20u;   /* clear EOP */
    }

    *FLASH_CR = 0x80u;   /* LOCK */
    *FLAG_ADDR = 0xCAFEBABEu;
    for(;;);
}
