#pragma once

#include "WVariant.h"

#define VARIANT_MCK (64000000ul)
#define USE_LFXO

#define PINS_COUNT 48
#define NUM_DIGITAL_PINS 48
#define NUM_ANALOG_INPUTS 1
#define NUM_ANALOG_OUTPUTS 0

#define PIN_LED1 (32 + 3)
#define LED_BLUE PIN_LED1
#define LED_GREEN PIN_LED1
#define LED_STATE_ON 0

#define PIN_BUTTON1 (32 + 10)

#define WIRE_INTERFACES_COUNT 2
#define PIN_WIRE_SDA 26
#define PIN_WIRE_SCL 27
#define PIN_WIRE1_SDA 16
#define PIN_WIRE1_SCL 13

#define SPI_INTERFACES_COUNT 2
#define PIN_SPI_MISO 23
#define PIN_SPI_MOSI 22
#define PIN_SPI_SCK 19
#define PIN_SPI_SS 24
#define SS PIN_SPI_SS
#define PIN_SPI1_MISO 255
#define PIN_SPI1_MOSI 41
#define PIN_SPI1_SCK 40

#define PIN_SERIAL1_RX (32 + 5)
#define PIN_SERIAL1_TX (32 + 7)

#define BATTERY_PIN 4
#define PIN_A0 BATTERY_PIN
#define ADC_RESOLUTION 14

extern const uint32_t g_ADigitalPinMap[];

#ifdef __cplusplus
extern "C" {
#endif
void initVariant(void);
void variant_shutdown(void);
#ifdef __cplusplus
}
#endif
