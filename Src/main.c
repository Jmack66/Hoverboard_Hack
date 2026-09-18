/*
 * Motor control firmware for MM32SPIN05PF hoverboard sideboards.
 * 6-step trapezoidal commutation via TIM1 complementary PWM.
 * ESP32 sends speed commands over UART1 (PB6/PB4).
 *
 * MM32SPIN05PF LQFP32 pin assignments (same as GD32F130K6):
 *   TIM1 CH1/CH1N : PA8 (hi-A), PA7  (lo-A)
 *   TIM1 CH2/CH2N : PA9 (hi-B), PB0  (lo-B)
 *   TIM1 CH3/CH3N : PA10 (hi-C), PB1 (lo-C)
 *   Hall A/B/C    : PC15 / PC13 / PC14
 *   UART1 TX/RX   : PB6 / PB4 @ 115200
 *
 * GPIO, UART, and TIM1 all use direct register writes.
 * GD32 SPL is retained only for rcu_periph_clock_enable and systick_clksource_set.
 *
 * ESP32 frame (3 bytes): [0xAB][int8_t duty -100..+100][XOR checksum]
 *   duty > 0 â†’ forward, duty < 0 â†’ reverse, 0 â†’ coast stop
 */

#include "gd32f1x0.h"
#include <stdint.h>
#include <string.h>

/* â”€â”€ MM32SPIN05PF GPIO registers (STM32F1-style CRL/CRH, 4-bit per pin) â”€â”€ */
#define MM32_GPIOA_BASE  0x48000000UL
#define MM32_GPIOB_BASE  0x48000400UL
#define MM32_GPIOC_BASE  0x48000800UL
#define MM32_GPIOD_BASE  0x48000C00UL

/* CRL/CRH 4-bit field values:
 *   bits[1:0] MODE: 00=input, 01=50MHz out, 10=20MHz out, 11=10MHz out
 *   bits[3:2] CNF (output): 00=PP, 01=OD, 10=AF-PP, 11=AF-OD
 *   bits[3:2] CNF (input):  00=analog, 01=float, 10=pull, 11=rsvd
 */
#define GPIO_CNF_AF_PP_50   0xBU   /* 1011: AF push-pull 50 MHz (MODE=11, CNF=10)  */
#define GPIO_CNF_IN_PULL    0x8U   /* 1000: input with pull      */

#define MM32_GPIO_CRL(p)    (*(volatile uint32_t*)((p) + 0x00))
#define MM32_GPIO_CRH(p)    (*(volatile uint32_t*)((p) + 0x04))
#define MM32_GPIO_IDR(p)    (*(volatile uint32_t*)((p) + 0x08))
#define MM32_GPIO_ODR(p)    (*(volatile uint32_t*)((p) + 0x0C))
#define MM32_GPIO_BSRR(p)   (*(volatile uint32_t*)((p) + 0x10))
/* MM32SPIN05PF GPIO AF register layout (confirmed from reference firmware behaviour):
 *   AFRL/AFSEL0 at +0x20 â€” pins 0-7  (4 bits each, same encoding as STM32F0)
 *   AFRH/AFSEL1 at +0x24 â€” pins 8-15
 * +0x1C is a different register; writing it disrupts UART â€” do not touch it. */
#define MM32_GPIO_AFRL(p)   (*(volatile uint32_t*)((p) + 0x20))
#define MM32_GPIO_AFRH(p)   (*(volatile uint32_t*)((p) + 0x24))

/* â”€â”€ MM32SPIN05PF UART1 registers (at 0x40013800, same bus address as GD32 USART0) â”€â”€ */
#define MM32_UART1_BASE  0x40013800UL

#define UART1_TDR    (*(volatile uint32_t*)(MM32_UART1_BASE + 0x00))
#define UART1_RDR    (*(volatile uint32_t*)(MM32_UART1_BASE + 0x04))
#define UART1_CSR    (*(volatile uint32_t*)(MM32_UART1_BASE + 0x08))
#define UART1_GCR    (*(volatile uint32_t*)(MM32_UART1_BASE + 0x18))
#define UART1_CCR    (*(volatile uint32_t*)(MM32_UART1_BASE + 0x1C))
#define UART1_BRR    (*(volatile uint32_t*)(MM32_UART1_BASE + 0x20))
#define UART1_FRA    (*(volatile uint32_t*)(MM32_UART1_BASE + 0x24))

/* CSR status bits */
#define UART_CSR_TXC     (1U << 0)   /* transmit complete          */
#define UART_CSR_RXAVL   (1U << 1)   /* receive data available     */
#define UART_CSR_TXFULL  (1U << 2)   /* transmit buffer full       */
#define UART_CSR_TXEPT   (1U << 3)   /* transmit buffer empty      */

/* GCR control bits */
#define UART_GCR_UARTEN  (1U << 0)   /* UART enable                */
#define UART_GCR_RX      (1U << 3)   /* receive enable             */
#define UART_GCR_TX      (1U << 4)   /* transmit enable            */

/* CCR config bits */
#define UART_CCR_CHAR_8B (3U << 4)   /* 8-bit word length          */

/* â”€â”€ TIM1 (= GD32 TIMER0) direct register writes â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
#define MM32_TIM1_BASE  0x40012C00UL

#define TIM1_CR1    (*(volatile uint32_t*)(MM32_TIM1_BASE + 0x00))
#define TIM1_CR2    (*(volatile uint32_t*)(MM32_TIM1_BASE + 0x04))
#define TIM1_EGR    (*(volatile uint32_t*)(MM32_TIM1_BASE + 0x14))
#define TIM1_CCMR1  (*(volatile uint32_t*)(MM32_TIM1_BASE + 0x18))
#define TIM1_CCMR2  (*(volatile uint32_t*)(MM32_TIM1_BASE + 0x1C))
#define TIM1_CCER   (*(volatile uint32_t*)(MM32_TIM1_BASE + 0x20))
#define TIM1_PSC    (*(volatile uint32_t*)(MM32_TIM1_BASE + 0x28))
#define TIM1_ARR    (*(volatile uint32_t*)(MM32_TIM1_BASE + 0x2C))
#define TIM1_RCR    (*(volatile uint32_t*)(MM32_TIM1_BASE + 0x30))
#define TIM1_CCR1   (*(volatile uint32_t*)(MM32_TIM1_BASE + 0x34))
#define TIM1_CCR2   (*(volatile uint32_t*)(MM32_TIM1_BASE + 0x38))
#define TIM1_CCR3   (*(volatile uint32_t*)(MM32_TIM1_BASE + 0x3C))
#define TIM1_BDTR   (*(volatile uint32_t*)(MM32_TIM1_BASE + 0x44))

/* CCER enable bits: CCxE = main output, CCxNE = complementary output */
#define TIM1_CCER_CC1E   (1U <<  0)
#define TIM1_CCER_CC1NE  (1U <<  2)
#define TIM1_CCER_CC2E   (1U <<  4)
#define TIM1_CCER_CC2NE  (1U <<  6)
#define TIM1_CCER_CC3E   (1U <<  8)
#define TIM1_CCER_CC3NE  (1U << 10)

/* â”€â”€ tunables â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
#define PWM_FREQ_HZ      20000
#define UART_BAUD        115200
#define RAMP_STEP        5          /* duty units per 1ms tick */
#define WATCHDOG_MS      500        /* coast to stop if no command this long */

/* Hall sensor pins â€” from ailife8881 git repo pin table:
 *   Hall A = PC15  (GPIOC bit 15)
 *   Hall B = PC13  (GPIOC bit 13)
 *   Hall C = PC14  (GPIOC bit 14) */
#define HALL_A_BIT       15U   /* PC15 */
#define HALL_B_BIT       13U   /* PC13 */
#define HALL_C_BIT       14U   /* PC14 */

/* â”€â”€ derived constants â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
/* SystemCoreClock = 72 MHz (HSI at full speed â€” see system_mm32.c).
 * All divisions resolved at compile time: avoids __udivsi3 on Cortex-M0. */
#define PWM_PERIOD  1800U  /* 72 000 000 / (2 Ã— 20 000) */
#define DUTY_MAX    1800   /* == PWM_PERIOD */

/* â”€â”€ commutation table â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
/* phase_drive[hall_state] = {ch0_mode, ch1_mode, ch2_mode}
 * mode: +1 = active positive (PWM), -1 = active negative (GND), 0 = float */
typedef struct { int8_t a, b, c; } step_t;

/* Forward direction commutation (may need Aâ†”B or similar swap if motor runs wrong way) */
static const step_t STEP_FWD[8] = {
    { 0,  0,  0},  /* 0: invalid */
    {+1, -1,  0},  /* 1 (001): A+ B- */
    { 0, +1, -1},  /* 2 (010): B+ C- */
    {+1,  0, -1},  /* 3 (011): A+ C- */
    {-1,  0, +1},  /* 4 (100): C+ A- */
    { 0, -1, +1},  /* 5 (101): C+ B- */
    {-1, +1,  0},  /* 6 (110): B+ A- */
    { 0,  0,  0},  /* 7: invalid */
};

/* â”€â”€ state â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
static volatile int32_t  g_target_duty  = 0;   /* -DUTY_MAX .. +DUTY_MAX */
static volatile int32_t  g_actual_duty  = 0;
static volatile uint32_t g_ms_tick      = 0;
static volatile uint32_t g_last_cmd_ms  = 0;
static volatile uint8_t  g_test_all     = 0;   /* 't' test: hold all 6 outputs at 50% */

/* UART receive ring buffer */
#define RX_BUF 16
static volatile uint8_t  rx_buf[RX_BUF];
static volatile uint8_t  rx_head = 0, rx_tail = 0;

/* â”€â”€ forward declarations â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
static void rcu_config(void);
static void gpio_config(void);
static void timer0_config(void);
static void usart1_config(void);
static void systick_config(void);
static void apply_step(const step_t *s, uint16_t duty);
static void all_off(void);
static uint8_t read_hall(void);
static void uart_send_byte(uint8_t b);
static void uart_send_str(const char *s);
static void uart_send_int(int32_t v);
static void uart_send_hex(uint32_t v);
static void process_uart(void);

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

int main(void)
{
    rcu_config();
    systick_config();       /* first â€” so g_ms_tick runs regardless of UART */
    gpio_config();
    usart1_config();
    uart_send_str("UART OK\r\n");
    timer0_config();
    uart_send_str("TIM OK\r\nMM32SPIN05PF motor ctrl init\r\n");

    uint32_t last_ms   = 0;
    uint32_t last_hb   = 0;   /* last heartbeat tick */

    while (1) {
        /* â”€â”€ 1ms ramp + watchdog â”€â”€ */
        uint32_t now = g_ms_tick;
        if (now != last_ms) {
            last_ms = now;

            /* watchdog: no command â†’ coast stop */
            if ((now - g_last_cmd_ms) > WATCHDOG_MS)
                g_target_duty = 0;

            /* ramp actual toward target */
            if (g_actual_duty < g_target_duty)
                g_actual_duty = (g_actual_duty + RAMP_STEP < g_target_duty)
                                ? g_actual_duty + RAMP_STEP : g_target_duty;
            else if (g_actual_duty > g_target_duty)
                g_actual_duty = (g_actual_duty - RAMP_STEP > g_target_duty)
                                ? g_actual_duty - RAMP_STEP : g_target_duty;
        }

        /* â”€â”€ 1 Hz heartbeat â”€â”€ */
        if ((now - last_hb) >= 1000) {
            uint8_t hall = read_hall();
            uint8_t a = hall & 1U;
            uint8_t b = (hall >> 1) & 1U;
            uint8_t c = (hall >> 2) & 1U;

            last_hb = now;
            uart_send_str("tick=");
            uart_send_int((int32_t)now);
            uart_send_str(" hall=");
            uart_send_int(hall);
            uart_send_str(" A=");
            uart_send_int(a);
            uart_send_str(" B=");
            uart_send_int(b);
            uart_send_str(" C=");
            uart_send_int(c);
            uart_send_str(" csr=");
            uart_send_hex(UART1_CSR);
            uart_send_str("\r\n");
            /* Kick the UART RX back to life if it got stuck in an error/overrun state */
            UART1_GCR &= ~(UART_GCR_RX);
            UART1_GCR |=  (UART_GCR_RX);
        }

        /* â”€â”€ parse incoming UART commands â”€â”€ */
        process_uart();

        /* â”€â”€ 6-step commutation â”€â”€ */
        if (g_test_all) {
            TIM1_CCR1 = PWM_PERIOD / 2;
            TIM1_CCR2 = PWM_PERIOD / 2;
            TIM1_CCR3 = PWM_PERIOD / 2;
            TIM1_CCER = 0x0555U;
            continue;
        }

        if (g_actual_duty == 0) {
            all_off();
            continue;
        }

        uint8_t hall = read_hall();

        if (hall == 0 || hall == 7) {
            /* invalid hall state - fault */
            all_off();
            uart_send_str("!HALL\r\n");
            uart_send_int((int32_t)hall);
            uart_send_str("\r\n");
            continue;
        }

        int32_t duty_abs = (g_actual_duty < 0) ? -g_actual_duty : g_actual_duty;
        uint16_t pwm_val = (uint16_t)(duty_abs > DUTY_MAX ? DUTY_MAX : duty_abs);

        if (g_actual_duty > 0) {
            apply_step(&STEP_FWD[hall], pwm_val);
        } else {
            /* reverse: invert all signs */
            step_t rev = { -STEP_FWD[hall].a, -STEP_FWD[hall].b, -STEP_FWD[hall].c };
            apply_step(&rev, pwm_val);
        }
    }
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
 * Clock configuration
 * â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */
static void rcu_config(void)
{
    rcu_periph_clock_enable(RCU_GPIOA);
    rcu_periph_clock_enable(RCU_GPIOB);
    rcu_periph_clock_enable(RCU_GPIOC);
    rcu_periph_clock_enable(RCU_GPIOD);
    rcu_periph_clock_enable(RCU_TIMER0);
    rcu_periph_clock_enable(RCU_USART0);
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
 * GPIO configuration â€” direct MM32SPIN05PF register writes.
 *
 * MM32 uses STM32F1-style CRL/CRH (4-bit per pin) instead of
 * GD32/STM32F0-style MODER/OTYPER.  AFRL/AFRH are the same
 * offset (0x20/0x24) and encoding as GD32, so AF numbers match.
 *
 *   CRL governs pins 0â€“7,  field for pin N at bits [(N*4)+3 : N*4]
 *   CRH governs pins 8â€“15, field for pin N-8 at bits [((N-8)*4)+3 : (N-8)*4]
 *   AFRL/AFRH: 4 bits per pin, pin N in AFRL at bits [(N*4)+3:N*4] (0-7),
 *              pin N in AFRH at bits [((N-8)*4)+3:(N-8)*4] (8-15).
 * â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */
static void gpio_config(void)
{
    uint32_t v;

    /* â”€â”€ GPIOA â”€â”€ */

    /* CRL: PA7=input-float, PA6=input-float */
    v = MM32_GPIO_CRL(MM32_GPIOA_BASE);
    v &= ~((0xFU << 28) | (0xFU << 24));
    v |=   (0x4U << 28) | (0x4U << 24);
    MM32_GPIO_CRL(MM32_GPIOA_BASE) = v;
    /* PA5: OFF pin — drive HIGH to keep gate drivers enabled */
    v = MM32_GPIO_CRL(MM32_GPIOA_BASE);
    v &= ~(0xFU << 20);      /* clear PA5 field */
    v |=  (0x1U << 20);      /* output PP 50MHz */
    MM32_GPIO_CRL(MM32_GPIOA_BASE) = v;

    MM32_GPIO_ODR(MM32_GPIOA_BASE) |= (1U << 5);  /* PA5 HIGH */

    /* CRH: PA8=AF-PP-50MHz, PA9=AF-PP-50MHz, PA10=AF-PP-50MHz (TIM1 CH1/2/3) */
    v = MM32_GPIO_CRH(MM32_GPIOA_BASE);
    v &= ~((0xFU << 8) | (0xFU << 4) | (0xFU << 0));
    v |=   (GPIO_CNF_AF_PP_50 << 8)   /* PA10 TIM1 CH3 */
         | (GPIO_CNF_AF_PP_50 << 4)   /* PA9  TIM1 CH2 */
         | (GPIO_CNF_AF_PP_50 << 0);  /* PA8  TIM1 CH1 */
    MM32_GPIO_CRH(MM32_GPIOA_BASE) = v;

    /* AFRL (+0x20): clear PA7 AF (floating input, no function needed) */
    v = MM32_GPIO_AFRL(MM32_GPIOA_BASE);
    v &= ~(0xFU << 28);
    MM32_GPIO_AFRL(MM32_GPIOA_BASE) = v;

    /* AFRH (+0x24): PA8=AF2, PA9=AF2, PA10=AF2 (TIM1 CH1/2/3) */
    v = MM32_GPIO_AFRH(MM32_GPIOA_BASE);
    v &= ~((0xFU << 8) | (0xFU << 4) | (0xFU << 0));
    v |=   (2U << 8) | (2U << 4) | (2U << 0);
    MM32_GPIO_AFRH(MM32_GPIOA_BASE) = v;

    /* â”€â”€ GPIOB â”€â”€ */

    /* CRL: PB2=output-PP (power latch HIGH), PB4=input-pull (UART1 RX),
     *      PB6=AF-PP-50MHz (UART1 TX), PB0/PB1 input float */
    v = MM32_GPIO_CRL(MM32_GPIOB_BASE);
    v &= ~((0xFU << 24) | (0xFU << 20) | (0xFU << 16) | (0xFU << 12)
         | (0xFU <<  8) | (0xFU <<  4) | (0xFU <<  0));
    v |=   (GPIO_CNF_AF_PP_50 << 24)  /* PB6 UART1 TX  */
         | (0x4U              << 20)  /* PB5 input float */
         | (GPIO_CNF_IN_PULL  << 16)  /* PB4 UART1 RX  */
         | (0x4U              << 12)  /* PB3 input float */
         | (0x1U              <<  8)  /* PB2 output PP 50MHz â€” power latch */
         | (0x4U              <<  4)  /* PB1 input float */
         | (0x4U              <<  0); /* PB0 input float */
    MM32_GPIO_CRL(MM32_GPIOB_BASE) = v;
    /* PB2 HIGH = latch board power on; PB4 pull-up for UART RX idle */
    MM32_GPIO_ODR(MM32_GPIOB_BASE) |= (1U << 2) | (1U << 4);

    /* CRH: PB10=input-float, PB13/14/15=AF-PP-50MHz (TIM1 CH1N/2N/3N) */
    v = MM32_GPIO_CRH(MM32_GPIOB_BASE);
    v &= ~((0xFU << 28) | (0xFU << 24) | (0xFU << 20) | (0xFU << 8));
    v |=   (GPIO_CNF_AF_PP_50 << 28)  /* PB15 TIM1 CH3N */
         | (GPIO_CNF_AF_PP_50 << 24)  /* PB14 TIM1 CH2N */
         | (GPIO_CNF_AF_PP_50 << 20)  /* PB13 TIM1 CH1N */
         | (0x4U              <<  8); /* PB10 input float */
    MM32_GPIO_CRH(MM32_GPIOB_BASE) = v;

    /* AFRL (+0x20): PB4=AF3 (UART1 RX), PB6=AF0 (UART1 TX) */
    v = MM32_GPIO_AFRL(MM32_GPIOB_BASE);
    v &= ~((0xFU << 24) | (0xFU << 16) | (0xFU << 4) | (0xFU << 0));
    v |=   (3U << 16);  /* PB4=AF3 */
    MM32_GPIO_AFRL(MM32_GPIOB_BASE) = v;

    /* AFRH (+0x24): PB13=AF2, PB14=AF2, PB15=AF2 (TIM1 CH1N/2N/3N) */
    v = MM32_GPIO_AFRH(MM32_GPIOB_BASE);
    v &= ~((0xFU << 28) | (0xFU << 24) | (0xFU << 20));
    v |=   (2U << 28) | (2U << 24) | (2U << 20);
    MM32_GPIO_AFRH(MM32_GPIOB_BASE) = v;

    /* â”€â”€ GPIOC â”€â”€ */

    /* CRH: PC13/14/15 all as floating inputs (no pull-up, like trondin firmware) */
    v = MM32_GPIO_CRH(MM32_GPIOC_BASE);
    v &= ~((0xFU << 28) | (0xFU << 24) | (0xFU << 20));
    v |=   (0x4U << 28)   /* PC15 input float */
         | (0x4U << 24)   /* PC14 input float */
         | (0x4U << 20);  /* PC13 input float */
    MM32_GPIO_CRH(MM32_GPIOC_BASE) = v;
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
 * TIM1 - center-aligned complementary PWM, all outputs off initially.
 * Direct register writes â€” no GD32 timer SPL.
 * â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */
static void timer0_config(void)
{
    TIM1_CR1   = 0;           /* stop counter while configuring */
    TIM1_CR2   = 0;            /* CCPC=0: CCMR+CCER writes take effect immediately */
    /* CH1, CH2, CH3: PWM mode 1 (OCM=110), preload enable (OCxPE=1) */
    TIM1_CCMR1 = 0x6868U;
    TIM1_CCMR2 = 0x0068U;
    TIM1_CCER  = 0;           /* all outputs disabled */
    TIM1_PSC   = 0;           /* no prescaler */
    TIM1_ARR   = PWM_PERIOD - 1;
    TIM1_RCR   = 0;
    TIM1_CCR1  = 0;
    TIM1_CCR2  = 0;
    TIM1_CCR3  = 0;
    /* BDTR: MOE=1, AOE=1, OSSR=1, OSSI=1 (disabled outputs driven to idle-low), DTGâ‰ˆ1Âµs */
    TIM1_BDTR  = (1U << 15) | (1U << 14) | (1U << 11) | (1U << 10) | 72U;
    TIM1_EGR   = 1U;          /* UG: latch PSC/ARR/RCR into shadow registers */
    /* CMS=11 (center-aligned 3), ARPE=1, URS=1, CEN=1 */
    TIM1_CR1   = (3U << 5) | (1U << 7) | (1U << 2) | 1U;
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
 * UART1 - PB6 TX / PB4 RX, 115200 8N1
 *
 * MM32SPIN05PF UART register layout (completely different from GD32 USART):
 *   GCR (0x18): UARTEN|RX|TX enable bits
 *   CCR (0x1C): word length, stop bits, parity
 *   BRR (0x20): integer baud divisor (SYSCLK / baud)
 *   FRA (0x24): fractional baud divisor Ã— 16
 * â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */
static void usart1_config(void)
{
    /* Reset: clear GCR to disable UART while configuring */
    UART1_GCR = 0;

    /* 8N1: CHAR_8B (bits 5:4 = 11), no parity, 1 stop bit */
    UART1_CCR = UART_CCR_CHAR_8B;

    /* MM32 baud divider = BRR*16 + FRA â†’ actual_baud = clk / (BRR*16 + FRA)
     * 72 000 000 / 115 200 = 625 exactly â†’ BRR=39, FRA=1 â†’ 115 200 baud (exact) */
    UART1_BRR = 39;
    UART1_FRA = 1;

    UART1_GCR = UART_GCR_UARTEN | UART_GCR_TX | UART_GCR_RX;
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
 * SysTick - 1ms tick
 * â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */
static void systick_config(void)
{
    systick_clksource_set(SYSTICK_CLKSOURCE_HCLK);
    SysTick->LOAD = 71999;  /* 72 000 000 / 1000 - 1 */
    SysTick->VAL  = 0;
    SysTick->CTRL = SysTick_CTRL_CLKSOURCE_Msk |
                    SysTick_CTRL_TICKINT_Msk    |
                    SysTick_CTRL_ENABLE_Msk;
}

void SysTick_Handler(void)
{
    g_ms_tick++;
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
 * Motor helpers
 * â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

static uint8_t read_hall(void)
{
    uint32_t inc = MM32_GPIO_IDR(MM32_GPIOC_BASE);
    /* Read raw pin levels â€” 0 when sensor active (active-low, pulled down by magnet).
     * STEP_FWD is indexed by this raw encoding, matching the reference firmware convention. */
    uint8_t a = (inc >> HALL_A_BIT) & 1U;  /* PC15 */
    uint8_t b = (inc >> HALL_B_BIT) & 1U;  /* PC13 */
    uint8_t c = (inc >> HALL_C_BIT) & 1U;  /* PC14 */
    return (c << 2) | (b << 1) | a;
}

/*
 * Apply a commutation step.
 * mode +1: active-positive â€” PWM on both main and complementary outputs
 * mode  0: float â€” both outputs disabled
 * mode -1: active-negative â€” force main low, complementary (low-side) ON
 */
static void apply_channel(uint16_t ch, int8_t mode, uint16_t duty)
{
    /* OCM: PWM mode 1 (110=0x6) for +1, forced-active (101=0x5) for -1.
     * With CCxE=0/CCxNE=1: OC1N = OCxREF XOR CCxNP. forced-inactive (0x4) gives
     * OCxREF=0 â†’ OC1N=0 â†’ low-side OFF (wrong). forced-active (0x5) gives
     * OCxREF=1 â†’ OC1N=1 â†’ low-side fully ON (correct return path). */
    uint32_t ocm = (mode == -1) ? 0x5U : 0x6U;

    switch (ch) {
    case 0:  /* TIM1 CH1: CCMR1[6:4], CCER bits 0(CCxE) and 2(CCxNE) */
        TIM1_CCMR1 = (TIM1_CCMR1 & ~0x0070U) | (ocm << 4);
        TIM1_CCR1  = (mode == +1) ? duty : 0U;
        if      (mode == +1) TIM1_CCER = (TIM1_CCER & ~0x0005U) | 0x0001U;  /* CCxE only */
        else if (mode == -1) TIM1_CCER = (TIM1_CCER & ~0x0005U) | 0x0004U;  /* CCxNE only */
        else                 TIM1_CCER &= ~0x0005U;
        break;
    case 1:  /* TIM1 CH2: CCMR1[14:12], CCER bits 4(CCxE) and 6(CCxNE) */
        TIM1_CCMR1 = (TIM1_CCMR1 & ~0x7000U) | (ocm << 12);
        TIM1_CCR2  = (mode == +1) ? duty : 0U;
        if      (mode == +1) TIM1_CCER = (TIM1_CCER & ~0x0050U) | 0x0010U;  /* CCxE only */
        else if (mode == -1) TIM1_CCER = (TIM1_CCER & ~0x0050U) | 0x0040U;  /* CCxNE only */
        else                 TIM1_CCER &= ~0x0050U;
        break;
    case 2:  /* TIM1 CH3: CCMR2[6:4], CCER bits 8(CCxE) and 10(CCxNE) */
        TIM1_CCMR2 = (TIM1_CCMR2 & ~0x0070U) | (ocm << 4);
        TIM1_CCR3  = (mode == +1) ? duty : 0U;
        if      (mode == +1) TIM1_CCER = (TIM1_CCER & ~0x0500U) | 0x0100U;  /* CCxE only */
        else if (mode == -1) TIM1_CCER = (TIM1_CCER & ~0x0500U) | 0x0400U;  /* CCxNE only */
        else                 TIM1_CCER &= ~0x0500U;
        break;
    }
}

static void apply_step(const step_t *s, uint16_t duty)
{
    apply_channel(TIMER_CH_0, s->a, duty);
    apply_channel(TIMER_CH_1, s->b, duty);
    apply_channel(TIMER_CH_2, s->c, duty);
    TIM1_EGR = (1U << 5);  /* COMG: harmless with CCPC=0; kept for compatibility */
}

static void all_off(void)
{
    TIM1_CCER &= ~(TIM1_CCER_CC1E | TIM1_CCER_CC1NE |
                   TIM1_CCER_CC2E | TIM1_CCER_CC2NE |
                   TIM1_CCER_CC3E | TIM1_CCER_CC3NE);
    TIM1_EGR = (1U << 5);  /* COMG: harmless with CCPC=0 */
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
 * UART helpers
 * â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

static void uart_send_hex(uint32_t v)
{
    static const char hex[] = "0123456789ABCDEF";
    uart_send_byte('0'); uart_send_byte('x');
    for (int8_t i = 28; i >= 0; i -= 4)
        uart_send_byte((uint8_t)hex[(v >> i) & 0xF]);
}

static void uart_send_byte(uint8_t b)
{
    for (volatile uint32_t i = 0; i < 2000; i++);
    UART1_TDR = b;
}

static void uart_send_str(const char *s)
{
    while (*s) uart_send_byte((uint8_t)*s++);
}

static void uart_send_int(int32_t v)
{
    /* Subtract powers of 10 â€” no division, avoids __udivsi3 on Cortex-M0. */
    static const uint32_t p10[] = {
        1000000000UL, 100000000UL, 10000000UL, 1000000UL,
        100000UL, 10000UL, 1000UL, 100UL, 10UL, 1UL
    };
    if (v < 0) { uart_send_byte('-'); v = -v; }
    if (v == 0) { uart_send_byte('0'); return; }
    uint32_t u = (uint32_t)v;
    uint8_t leading = 1;
    for (uint8_t j = 0; j < 10; j++) {
        uint8_t d = 0;
        while (u >= p10[j]) { u -= p10[j]; d++; }
        if (d || !leading) { uart_send_byte((uint8_t)('0' + d)); leading = 0; }
    }
}

/*
 * Frame from ESP32: [0xAB][int8_t speed_pct -100..+100][XOR checksum]
 * checksum = 0xAB ^ (uint8_t)speed_pct
 *
 * Responds with: "OK hall=N duty=N\r\n" for debug
 *
 * Also accepts bare ASCII:
 *   '+' â†’ +30 duty (slow forward, useful for bench test)
 *   '-' â†’ -30 duty
 *   '0' â†’ stop
 *   'd' â†’ dump hall + duty state
 */
static void process_uart(void)
{
    /* Poll CSR.RXAVL (bit 1) â€” MM32 UART receive available flag */
    if (!(UART1_CSR & UART_CSR_RXAVL))
        return;

    uint8_t b = (uint8_t)UART1_RDR;

    static uint8_t state = 0;
    static uint8_t pkt_speed = 0;

    switch (state) {
    case 0:
        if (b == 0xAB) { state = 1; break; }
        /* ASCII shortcuts for bench testing */
        if (b == '+') {
            g_target_duty =  600; g_last_cmd_ms = g_ms_tick;
            uart_send_str("FWD33\r\n");
        }
        if (b == '-') {
            g_target_duty = -600; g_last_cmd_ms = g_ms_tick;
            uart_send_str("REV33\r\n");
        }
        /* '1'..'9': set duty to N*200 (200..1800) for graduated testing */
        if (b >= '1' && b <= '9') {
            g_target_duty = (int32_t)(b - '0') * 200;
            g_last_cmd_ms = g_ms_tick;
            uart_send_str("DUTY="); uart_send_int(g_target_duty); uart_send_str("\r\n");
        }
        if (b == '0') { g_test_all = 0; g_target_duty = 0; g_last_cmd_ms = g_ms_tick; uart_send_str("STP\r\n"); }
        if (b == 'd') {
            uart_send_str("hall="); uart_send_int(read_hall());
            uart_send_str(" duty="); uart_send_int(g_actual_duty);
            uart_send_str("\r\nTIM1: ARR="); uart_send_hex(TIM1_ARR);
            uart_send_str(" BDTR="); uart_send_hex(TIM1_BDTR);
            uart_send_str(" CCER="); uart_send_hex(TIM1_CCER);
            uart_send_str(" CCR1="); uart_send_hex(TIM1_CCR1);
            uart_send_str("\r\nGPIO: A_CRH="); uart_send_hex(MM32_GPIO_CRH(MM32_GPIOA_BASE));
            uart_send_str(" A_AFRH="); uart_send_hex(MM32_GPIO_AFRH(MM32_GPIOA_BASE));
            uart_send_str("\r\n     B_CRH="); uart_send_hex(MM32_GPIO_CRH(MM32_GPIOB_BASE));
            uart_send_str(" B_AFRH="); uart_send_hex(MM32_GPIO_AFRH(MM32_GPIOB_BASE));
            uart_send_str("\r\n");
        }
        /* 'h' hall monitor: print hall state 30 times with ~50ms gaps.
         * Spin motor by hand while sending 'h' â€” look for clean 1â†’3â†’2â†’6â†’4â†’5 cycle. */
        if (b == 'h') {
            uart_send_str("HALL SCAN\r\n");
            for (uint8_t hi = 0; hi < 30; hi++) {
                for (volatile uint32_t d = 0; d < 3600000; d++) {}  /* ~50ms */

                uint32_t ina = MM32_GPIO_IDR(MM32_GPIOA_BASE);
                uint32_t inb = MM32_GPIO_IDR(MM32_GPIOB_BASE);
                uint32_t inc = MM32_GPIO_IDR(MM32_GPIOC_BASE);

                uart_send_str("A=");
                uart_send_hex(ina);
                uart_send_str(" B=");
                uart_send_hex(inb);
                uart_send_str(" C=");
                uart_send_hex(inc);
                uart_send_str("\r\n");    /* CORRECT */
            }
        }
        /* 't' brute-force test: hold all 6 TIM1 outputs at 50% â€” bypasses commutation */
        if (b == 't') {
            g_test_all = 1;
            uart_send_str("TEST-ALL\r\n");
        }
        /* 'o' open-loop: force-step through all 6 commutation positions ignoring hall.
         * Motor will twitch through 6 positions if TIM1 is actually driving the outputs. */
        if (b == 'o') {
            uart_send_str("OPENLOOP\r\n");
            static const step_t open_steps[6] = {
                {+1,-1, 0}, {+1, 0,-1}, { 0,+1,-1},
                {-1,+1, 0}, {-1, 0,+1}, { 0,-1,+1}
            };
            for (uint8_t si = 0; si < 6; si++) {
                apply_step(&open_steps[si], 1200);
                for (volatile uint32_t d = 0; d < 720000; d++);  /* ~10 ms */
            }
            all_off();
        }
        /* 'g' GPIO test: take PA8+PB14 away from TIM1 AF and drive them HIGH as plain GPIO.
         * If supply current jumps, the gate driver chain works but TIM1 AF routing is broken.
         * If nothing changes, the gate driver has an enable problem unrelated to AF. */
        if (b == 'g') {
            /* Stop TIM1 and zero all CCER */
            TIM1_CR1 &= ~1U;
            TIM1_CCER = 0;
            /* PA8 â†’ GPIO PP output HIGH (mode=01 cnf=00 â†’ nibble 0x1) */
            uint32_t v = MM32_GPIO_CRH(MM32_GPIOA_BASE);
            v &= ~(0xFU << 0);
            v |=  (0x1U << 0);
            MM32_GPIO_CRH(MM32_GPIOA_BASE) = v;
            MM32_GPIO_ODR(MM32_GPIOA_BASE) |= (1U << 8);   /* PA8 HIGH */
            /* PB14 â†’ GPIO PP output HIGH */
            v = MM32_GPIO_CRH(MM32_GPIOB_BASE);
            v &= ~(0xFU << 24);
            v |=  (0x1U << 24);
            MM32_GPIO_CRH(MM32_GPIOB_BASE) = v;
            MM32_GPIO_ODR(MM32_GPIOB_BASE) |= (1U << 14);  /* PB14 HIGH */
            uart_send_str("GPIO-HI: PA8+PB14 driven HIGH\r\n");
        }
        break;

    case 1:
        pkt_speed = b;
        state = 2;
        break;

    case 2: {
        uint8_t expected_cs = 0xAB ^ pkt_speed;
        if (b == expected_cs) {
            int8_t  pct = (int8_t)pkt_speed;
            g_target_duty = (int32_t)pct * 18;  /* pctÃ—DUTY_MAX/100 = pctÃ—1800/100 = pctÃ—18 */
            g_last_cmd_ms   = g_ms_tick;
        }
        state = 0;
        break;
    }

    default:
        state = 0;
    }
}
