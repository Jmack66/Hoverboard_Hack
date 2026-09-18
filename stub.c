#include <stdint.h>
#define REG(a) (*(volatile uint32_t*)(a))

void stub(void) {
    /* Feed + extend IWDG first */
    REG(0x40003000) = 0xAAAA;
    REG(0x40003000) = 0x5555;
    REG(0x40003004) = 6;
    REG(0x40003008) = 0x0FFF;
    REG(0x40003000) = 0xAAAA;

    /* Unlock flash KEYR */
    REG(0x40022004) = 0x45670123;
    REG(0x40022004) = 0xCDEF89AB;

    /* Signal done to host */
    REG(0x20000100) = 0xCAFEBABE;

    while (1) { REG(0x40003000) = 0xAAAA; }  /* keep feeding WDT */
}
