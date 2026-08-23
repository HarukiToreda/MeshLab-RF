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

constexpr uint8_t GPS_CPU_RX = 39;
constexpr uint8_t GPS_CPU_TX = 37;
constexpr uint8_t GPS_STANDBY = 34;
constexpr uint8_t PERIPHERAL_POWER = 21;

constexpr uint32_t SURVEY_STORAGE_BYTES = 1024UL * 1024UL;
constexpr uint32_t SURVEY_STORAGE_HEADER_BYTES = 4096UL;
