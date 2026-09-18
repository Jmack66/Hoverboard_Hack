#!/bin/bash

REG_DIR="/Users/s1862353/Documents/GitHub/hover/Hoverboard-Firmware-Hack-Gen2.x-MM32/HoverBoardMindMotion/Library/MM32SPIN05/HAL_Lib/Inc"

# List of register header files to create
REG_FILES=(
    "reg_adc.h"
    "reg_common.h"
    "reg_comp.h"
    "reg_crc.h"
    "reg_dbg.h"
    "reg_div.h"
    "reg_dma.h"
    "reg_exti.h"
    "reg_flash.h"
    "reg_i2c.h"
    "reg_iwdg.h"
    "reg_pwr.h"
    "reg_rcc.h"
    "reg_spi.h"
    "reg_tim.h"
    "reg_uart.h"
    "reg_wwdg.h"
)

for file in "${REG_FILES[@]}"; do
    if [ ! -f "$REG_DIR/$file" ]; then
        # Convert filename to header guard (e.g., reg_adc.h -> __REG_ADC_H)
        guard=$(echo "$file" | tr 'a-z' 'A-Z' | sed 's/\.H/_H/' | sed 's/\.//')
        
        cat > "$REG_DIR/$file" << FILEEOF
/**
 * @file    $file
 * @brief   Register definitions for MM32SPIN05
 */

#ifndef _$guard
#define _$guard

/* Register bit definitions and masks */
/* Placeholder for actual register definitions */

#endif /* _$guard */
FILEEOF
        echo "Created: $file"
    fi
done
