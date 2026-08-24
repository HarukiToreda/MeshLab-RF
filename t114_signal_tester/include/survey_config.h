#pragma once

// These defaults match MeshLab RF's US915 LongFast test profile. Change the
// frequency before transmitting if the radios will be used in another region.
constexpr float SURVEY_FREQUENCY_MHZ = 906.875F;
constexpr uint32_t SURVEY_FREQUENCY_HZ = 906875000UL;
constexpr float SURVEY_BANDWIDTH_KHZ = 250.0F;
constexpr uint8_t SURVEY_SPREADING_FACTOR = 11;
constexpr uint8_t SURVEY_CODING_RATE = 5;
constexpr uint8_t SURVEY_SYNC_WORD = 0x12;
constexpr int8_t SURVEY_TX_POWER_DBM = 22;
constexpr uint16_t SURVEY_PREAMBLE_LENGTH = 16;
constexpr float SURVEY_TCXO_VOLTAGE = 1.8F;

constexpr uint32_t SURVEY_SEND_INTERVAL_MS = 5000;
constexpr uint32_t SURVEY_REPLY_TIMEOUT_MS = 12000;
constexpr uint32_t BASE_PACKET_SCREEN_HOLD_MS = 10000;
constexpr uint32_t BASE_STATUS_REFRESH_MS = 10000;
constexpr uint8_t USER_BUTTON_PIN = 42;
// Meshtastic uses a 1 ms debounce for this screened T114 button.
constexpr uint32_t BUTTON_DEBOUNCE_MS = 1;
// Matches Meshtastic's screened-device user-button long-press threshold.
constexpr uint32_t BUTTON_LONG_PRESS_MS = 500;
constexpr uint32_t MENU_TIMEOUT_MS = 30000;

// A position is trusted only after several consecutive fresh fixes meet these
// walking-survey limits. This prevents a first, stale, or visibly drifting fix
// from triggering a radio sample.
constexpr uint32_t GPS_MAX_FIX_AGE_MS = 1500;
constexpr uint8_t GPS_MIN_SATELLITES = 6;
constexpr uint16_t GPS_MAX_HDOP_CENTI = 200;
constexpr uint8_t GPS_REQUIRED_GOOD_FIXES = 5;
constexpr float GPS_MAX_WALK_SPEED_KMPH = 15.0F;
constexpr float GPS_JUMP_BASE_METERS = 8.0F;
constexpr float GPS_JUMP_METERS_PER_SECOND = 4.0F;
constexpr float GPS_MIN_SAMPLE_DISTANCE_METERS = 5.0F;

constexpr uint8_t LORA_CS = 24;
constexpr uint8_t LORA_DIO1 = 20;
constexpr uint8_t LORA_RESET = 25;
constexpr uint8_t LORA_BUSY = 17;

constexpr uint8_t TFT_CS = 11;
constexpr uint8_t TFT_DC = 12;
constexpr uint8_t TFT_MOSI = 41;
constexpr uint8_t TFT_SCK = 40;
constexpr uint8_t TFT_RESET = 2;
constexpr uint8_t TFT_POWER = 3;
constexpr uint8_t TFT_BACKLIGHT = 15;
// Landscape orientation with the USB/controls at the expected bottom edge.
// Use 1 to rotate the display back by 180 degrees.
constexpr uint8_t TFT_ROTATION = 3;

constexpr uint8_t GPS_CPU_RX = 37;
constexpr uint8_t GPS_CPU_TX = 39;
constexpr uint8_t GPS_STANDBY = 34;
constexpr uint8_t PERIPHERAL_POWER = 21;

constexpr uint8_t BATTERY_ADC_PIN = 4;
constexpr uint8_t BATTERY_ADC_ENABLE_PIN = 6;
constexpr uint8_t BATTERY_ADC_RESOLUTION_BITS = 12;
constexpr float BATTERY_ADC_REFERENCE_VOLTS = 3.0F;
constexpr float BATTERY_ADC_MULTIPLIER = 4.916F;
constexpr uint8_t BATTERY_ADC_SAMPLES = 8;
constexpr uint32_t BATTERY_READ_INTERVAL_MS = 5000;

constexpr uint8_t BUZZER_PIN = 33;
constexpr uint16_t BUZZER_SEND_FREQUENCY_HZ = 2600;
constexpr uint16_t BUZZER_RECEIVE_FREQUENCY_HZ = 2600;
constexpr uint16_t BUZZER_TONE_DURATION_MS = 35;
constexpr uint16_t BUZZER_TONE_GAP_MS = 25;

// Internal nRF52840 flash reserved by linker/nrf52840_s140_v6_survey.ld.
// The application ends at 0x4D000 and the log occupies 0x4D000..0xECFFF.
constexpr uint32_t SURVEY_STORAGE_START = 0x4D000UL;
constexpr uint32_t SURVEY_STORAGE_BYTES = 640UL * 1024UL;
constexpr uint32_t SURVEY_STORAGE_HEADER_BYTES = 4096UL;
