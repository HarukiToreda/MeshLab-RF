#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>
#include <Arduino.h>
#include <RadioLib.h>
#include <TinyGPS++.h>
#include <SPI.h>
#include <cstddef>
#include <cmath>
#include <cstring>
#include <nrf_gpio.h>
#include <nrf_sdm.h>
#include <nrf_soc.h>

#include "survey_config.h"
#include "survey_protocol.h"

#if !defined(SURVEY_ROLE_MOBILE) && !defined(SURVEY_ROLE_BASE)
#error "Select either the mobile or base PlatformIO environment"
#endif

volatile uint32_t gInternalFlashEvent = 0;

extern "C" void flash_nrf5x_event_cb(uint32_t event)
{
    if (event == NRF_EVT_FLASH_OPERATION_SUCCESS || event == NRF_EVT_FLASH_OPERATION_ERROR)
        gInternalFlashEvent = event;
}

namespace
{
#if defined(SURVEY_ROLE_MOBILE)
constexpr SurveyRole ROLE = SurveyRole::Mobile;
constexpr char ROLE_NAME[] = "MOBILE";
#else
constexpr SurveyRole ROLE = SurveyRole::Base;
constexpr char ROLE_NAME[] = "BASE";
#endif

constexpr uint32_t FLASH_SECTOR_BYTES = 4096;
constexpr uint8_t MAX_RADIO_PACKET_BYTES = 96;
constexpr uint8_t PENDING_COUNT = 4;
constexpr uint8_t DASHBOARD_ROW_COUNT = 9;
constexpr uint8_t BUZZER_QUEUE_CAPACITY = 4;
constexpr uint8_t BUTTON_EDGE_QUEUE_CAPACITY = 16;
constexpr uint8_t MENU_OPTION_COUNT = 7;
constexpr uint32_t SCREEN_TIMEOUT_OPTIONS_MS[] = {30000UL, 60000UL, 600000UL, 0UL};
constexpr uint8_t SCREEN_TIMEOUT_OPTION_COUNT =
    sizeof(SCREEN_TIMEOUT_OPTIONS_MS) / sizeof(SCREEN_TIMEOUT_OPTIONS_MS[0]);

TinyGPSPlus gps;
Adafruit_ST7789 display(&SPI1, TFT_CS, TFT_DC, TFT_RESET);
SX1262 radio = new Module(LORA_CS, LORA_DIO1, LORA_RESET, LORA_BUSY);

volatile bool radioInterrupt = false;
bool radioReady = false;
bool storageReady = false;
bool loggingEnabled = false;
uint64_t deviceId = 0;
uint32_t sessionId = 0;
uint16_t batteryMillivolts = 0;
uint32_t lastBatteryReadMs = 0;
bool batteryReadingValid = false;
bool batteryReadOnce = false;
bool soundEnabled = true;
bool gpsFixTrusted = false;
uint8_t gpsGoodFixCount = 0;
bool gpsCandidateValid = false;
double gpsCandidateLatitude = 0.0;
double gpsCandidateLongitude = 0.0;
uint32_t gpsCandidateMs = 0;
#if defined(SURVEY_ROLE_MOBILE)
uint32_t nextSequence = 0;
uint32_t lastSendMs = 0;
uint32_t lastGpsNoticeMs = 0;
bool lastProbePositionValid = false;
double lastProbeLatitude = 0.0;
double lastProbeLongitude = 0.0;
#else
uint32_t lastBasePacketMs = 0;
uint32_t lastBaseStatusMs = 0;
#endif

enum class MenuMode : uint8_t { Closed, Select, ConfirmErase, ConfirmPowerOff };
MenuMode menuMode = MenuMode::Closed;
uint8_t menuSelection = 0;
bool buttonRawDown = false;
bool buttonStableDown = false;
bool suppressButtonRelease = false;
bool buttonLongHandled = false;
uint8_t buttonHoldProgress = 0;
uint32_t buttonChangedMs = 0;
uint32_t buttonPressedMs = 0;
uint32_t lastMenuInteractionMs = 0;
volatile uint32_t buttonEdgeTimes[BUTTON_EDGE_QUEUE_CAPACITY] = {};
volatile uint8_t buttonEdgeLevels[BUTTON_EDGE_QUEUE_CAPACITY] = {};
volatile uint8_t buttonEdgeHead = 0;
volatile uint8_t buttonEdgeTail = 0;

struct PendingProbe {
    bool active;
    uint32_t sequence;
    uint32_t sentAtMs;
    uint32_t packetId;
    SurveyPosition position;
};

PendingProbe pending[PENDING_COUNT] = {};

enum class DashboardResponse : uint8_t { Waiting, Received, Missed, ReplySent, ReplyFailed, SendFailed };

struct DashboardRow {
    bool active;
    bool signalValid;
    uint32_t sequence;
    int16_t rssi;
    int16_t snrCenti;
    DashboardResponse response;
};

DashboardRow dashboardRows[DASHBOARD_ROW_COUNT] = {};
uint8_t dashboardRowCount = 0;
char dashboardStatus[22] = "READY";
uint16_t dashboardStatusColor = ST77XX_YELLOW;
char pausedGpsRendered[4][24] = {};
uint16_t pausedGpsRenderedColor[4] = {};
bool pausedGpsCacheValid = false;
bool controlCenterSelected = true;
bool controlStorageCacheValid = false;
bool controlStorageReadyRendered = false;
bool controlSoundRendered = false;
uint8_t controlScreenTimeoutRendered = UINT8_MAX;
uint32_t controlStorageSlotsRendered = UINT32_MAX;
bool controlCenterRendered = false;
uint32_t transientNoticeUntilMs = 0;
uint8_t screenTimeoutSelection = SCREEN_TIMEOUT_OPTION_COUNT - 1;
bool screenAwake = true;
uint32_t lastScreenInteractionMs = 0;

constexpr uint16_t BATTERY_OCV_MV[] = {4190, 4050, 3990, 3890, 3800, 3720, 3630, 3530, 3420, 3300, 3100};

const char *screenTimeoutMenuLabel()
{
    switch (screenTimeoutSelection) {
    case 0:
        return "SCREEN: 30 SEC";
    case 1:
        return "SCREEN: 60 SEC";
    case 2:
        return "SCREEN: 10 MIN";
    default:
        return "SCREEN: ALWAYS ON";
    }
}

const char *screenTimeoutShortLabel()
{
    switch (screenTimeoutSelection) {
    case 0:
        return "30S";
    case 1:
        return "60S";
    case 2:
        return "10M";
    default:
        return "ON";
    }
}

uint8_t batteryPercent(uint16_t millivolts)
{
    constexpr size_t pointCount = sizeof(BATTERY_OCV_MV) / sizeof(BATTERY_OCV_MV[0]);
    if (millivolts >= BATTERY_OCV_MV[0])
        return 100;
    if (millivolts <= BATTERY_OCV_MV[pointCount - 1])
        return 0;
    for (size_t index = 1; index < pointCount; ++index) {
        if (millivolts < BATTERY_OCV_MV[index])
            continue;
        const uint16_t high = BATTERY_OCV_MV[index - 1];
        const uint16_t low = BATTERY_OCV_MV[index];
        const float segment = static_cast<float>(millivolts - low) / static_cast<float>(high - low);
        const float percent = static_cast<float>((pointCount - 1 - index) * 10) + segment * 10.0F;
        return static_cast<uint8_t>(lround(percent));
    }
    return 0;
}

bool updateBatteryReading(bool force = false)
{
    const uint32_t now = millis();
    if (!force && batteryReadOnce && static_cast<uint32_t>(now - lastBatteryReadMs) < BATTERY_READ_INTERVAL_MS)
        return false;
    lastBatteryReadMs = now;
    batteryReadOnce = true;

    digitalWrite(BATTERY_ADC_ENABLE_PIN, HIGH);
    delay(10);
    analogRead(BATTERY_ADC_PIN); // Discard the first sample after enabling the divider.
    uint32_t raw = 0;
    for (uint8_t sample = 0; sample < BATTERY_ADC_SAMPLES; ++sample)
        raw += analogRead(BATTERY_ADC_PIN);
    digitalWrite(BATTERY_ADC_ENABLE_PIN, LOW);

    raw /= BATTERY_ADC_SAMPLES;
    const float millivolts = BATTERY_ADC_MULTIPLIER * BATTERY_ADC_REFERENCE_VOLTS * 1000.0F * raw /
                             static_cast<float>(1UL << BATTERY_ADC_RESOLUTION_BITS);
    const uint16_t measured = static_cast<uint16_t>(lround(millivolts));
    batteryReadingValid = measured >= 2500 && measured <= 5000;
    if (!batteryReadingValid) {
        batteryMillivolts = measured;
        return true;
    }
    batteryMillivolts = batteryMillivolts ? static_cast<uint16_t>((batteryMillivolts + measured) / 2) : measured;
    return true;
}

#if defined(SURVEY_ROLE_MOBILE)
uint16_t buzzerQueue[BUZZER_QUEUE_CAPACITY] = {};
uint8_t buzzerQueueHead = 0;
uint8_t buzzerQueueTail = 0;
uint8_t buzzerQueueCount = 0;
bool buzzerPlaying = false;
uint32_t buzzerStopMs = 0;
uint32_t buzzerNextStartMs = 0;

void queueBuzzerTone(uint16_t frequency)
{
    if (buzzerQueueCount >= BUZZER_QUEUE_CAPACITY)
        return;
    buzzerQueue[buzzerQueueTail] = frequency;
    buzzerQueueTail = (buzzerQueueTail + 1) % BUZZER_QUEUE_CAPACITY;
    ++buzzerQueueCount;
}

void serviceBuzzer()
{
    const uint32_t now = millis();
    if (buzzerPlaying && static_cast<int32_t>(now - buzzerStopMs) >= 0) {
        noTone(BUZZER_PIN);
        buzzerPlaying = false;
        buzzerNextStartMs = now + BUZZER_TONE_GAP_MS;
    }
    if (buzzerPlaying || !buzzerQueueCount || static_cast<int32_t>(now - buzzerNextStartMs) < 0)
        return;
    const uint16_t frequency = buzzerQueue[buzzerQueueHead];
    buzzerQueueHead = (buzzerQueueHead + 1) % BUZZER_QUEUE_CAPACITY;
    --buzzerQueueCount;
    tone(BUZZER_PIN, frequency);
    buzzerPlaying = true;
    buzzerStopMs = now + BUZZER_TONE_DURATION_MS;
}
#endif

uint32_t crc32Update(uint32_t crc, const uint8_t *data, size_t length)
{
    while (length--) {
        crc ^= *data++;
        for (uint8_t bit = 0; bit < 8; ++bit)
            crc = (crc >> 1) ^ (0xEDB88320UL & (0UL - (crc & 1UL)));
    }
    return crc;
}

uint32_t crc32(const void *data, size_t length)
{
    return ~crc32Update(0xFFFFFFFFUL, static_cast<const uint8_t *>(data), length);
}

bool allErased(const uint8_t *data, size_t length)
{
    while (length--) {
        if (*data++ != 0xFF)
            return false;
    }
    return true;
}

bool softDeviceEnabled()
{
    uint8_t enabled = 0;
    return sd_softdevice_is_enabled(&enabled) == NRF_SUCCESS && enabled;
}

bool waitForInternalFlash()
{
    const uint32_t deadline = millis() + 10000;
    while (static_cast<int32_t>(millis() - deadline) < 0) {
        const uint32_t callbackEvent = gInternalFlashEvent;
        if (callbackEvent == NRF_EVT_FLASH_OPERATION_SUCCESS)
            return true;
        if (callbackEvent == NRF_EVT_FLASH_OPERATION_ERROR)
            return false;

        uint32_t event = 0;
        while (sd_evt_get(&event) == NRF_SUCCESS) {
            if (event == NRF_EVT_FLASH_OPERATION_SUCCESS)
                return true;
            if (event == NRF_EVT_FLASH_OPERATION_ERROR)
                return false;
        }
        delay(1);
    }
    return false;
}

void waitForNvmc()
{
    while (NRF_NVMC->READY == NVMC_READY_READY_Busy)
        yield();
}

void erasePageWithNvmc(uint32_t address)
{
    NRF_NVMC->CONFIG = NVMC_CONFIG_WEN_Een << NVMC_CONFIG_WEN_Pos;
    waitForNvmc();
    NRF_NVMC->ERASEPAGE = address;
    waitForNvmc();
    NRF_NVMC->CONFIG = NVMC_CONFIG_WEN_Ren << NVMC_CONFIG_WEN_Pos;
    waitForNvmc();
}

void writeWordsWithNvmc(uint32_t address, const uint32_t *words, size_t wordCount)
{
    NRF_NVMC->CONFIG = NVMC_CONFIG_WEN_Wen << NVMC_CONFIG_WEN_Pos;
    waitForNvmc();
    volatile uint32_t *destination = reinterpret_cast<volatile uint32_t *>(address);
    for (size_t index = 0; index < wordCount; ++index) {
        destination[index] = words[index];
        waitForNvmc();
    }
    NRF_NVMC->CONFIG = NVMC_CONFIG_WEN_Ren << NVMC_CONFIG_WEN_Pos;
    waitForNvmc();
}

bool eraseInternalFlashPage(uint32_t address)
{
    if (address % FLASH_SECTOR_BYTES)
        return false;
    if (!softDeviceEnabled()) {
        erasePageWithNvmc(address);
        const uint32_t *page = reinterpret_cast<const uint32_t *>(address);
        for (size_t word = 0; word < FLASH_SECTOR_BYTES / sizeof(uint32_t); ++word) {
            if (page[word] != 0xFFFFFFFFUL)
                return false;
        }
        return true;
    }

    for (uint8_t attempt = 0; attempt < 20; ++attempt) {
        gInternalFlashEvent = 0;
        const uint32_t result = sd_flash_page_erase(address / FLASH_SECTOR_BYTES);
        if (result == NRF_SUCCESS)
            return waitForInternalFlash();
        if (result != NRF_ERROR_BUSY)
            return false;
        delay(1);
    }
    return false;
}

bool writeInternalFlashChunk(uint32_t address, const uint8_t *data, size_t length)
{
    if ((address & 3U) || (length & 3U) || !length || length > sizeof(SurveyRecord))
        return false;
    alignas(4) uint32_t words[sizeof(SurveyRecord) / sizeof(uint32_t)] = {};
    memcpy(words, data, length);

    if (!softDeviceEnabled()) {
        writeWordsWithNvmc(address, words, length / sizeof(uint32_t));
        return memcmp(reinterpret_cast<const void *>(address), words, length) == 0;
    }

    for (uint8_t attempt = 0; attempt < 20; ++attempt) {
        gInternalFlashEvent = 0;
        const uint32_t result =
            sd_flash_write(reinterpret_cast<uint32_t *>(address), words, length / sizeof(uint32_t));
        if (result == NRF_SUCCESS)
            return waitForInternalFlash() && memcmp(reinterpret_cast<const void *>(address), words, length) == 0;
        if (result != NRF_ERROR_BUSY)
            return false;
        delay(1);
    }
    return false;
}

bool writeInternalFlash(uint32_t address, const void *data, size_t length)
{
    const uint8_t *source = static_cast<const uint8_t *>(data);
    while (length) {
        const size_t untilPageEnd = FLASH_SECTOR_BYTES - address % FLASH_SECTOR_BYTES;
        const size_t chunk = min(length, untilPageEnd);
        if (!writeInternalFlashChunk(address, source, chunk))
            return false;
        address += chunk;
        source += chunk;
        length -= chunk;
    }
    return true;
}

void readInternalFlash(uint32_t address, void *destination, size_t length)
{
    memcpy(destination, reinterpret_cast<const void *>(address), length);
}

enum class StorageError : uint8_t { None, LayoutInvalid, FormatFailed, WriteFailed };

class SurveyStorage
{
  public:
    bool begin()
    {
        const uint32_t flashBytes = NRF_FICR->CODEPAGESIZE * NRF_FICR->CODESIZE;
        if (FLASH_SECTOR_BYTES != NRF_FICR->CODEPAGESIZE || SURVEY_STORAGE_START % FLASH_SECTOR_BYTES ||
            SURVEY_STORAGE_BYTES % FLASH_SECTOR_BYTES ||
            SURVEY_STORAGE_START + SURVEY_STORAGE_BYTES > flashBytes ||
            SURVEY_STORAGE_START + SURVEY_STORAGE_BYTES > 0xED000UL) {
            failure = StorageError::LayoutInvalid;
            return false;
        }
        partitionStart = SURVEY_STORAGE_START;

        StorageHeader header = {};
        readInternalFlash(partitionStart, &header, sizeof(header));
        const bool headerCrcValid = header.magic == SURVEY_STORAGE_MAGIC &&
                                    header.partitionBytes == SURVEY_STORAGE_BYTES &&
                                    header.crc32 == crc32(&header, offsetof(StorageHeader, crc32));
        const bool currentFormat = headerCrcValid && header.version == SURVEY_FORMAT_VERSION &&
                                   header.recordSize == sizeof(SurveyRecord);
        const bool legacyFormat = headerCrcValid && header.version == SURVEY_LEGACY_FORMAT_VERSION &&
                                  header.recordSize == sizeof(LegacySurveyRecordV1);
        if (!currentFormat && !legacyFormat) {
            if (allErased(reinterpret_cast<const uint8_t *>(&header), sizeof(header))) {
                if (format())
                    return true;
                failure = StorageError::FormatFailed;
                return false;
            }
            formatRequired = true;
            return true;
        }

        activeVersion = static_cast<uint8_t>(header.version);
        activeRecordSize = header.recordSize;
        appendEnabled = currentFormat;

        scan();
        return true;
    }

    bool format()
    {
        for (uint32_t address = partitionStart; address < partitionStart + SURVEY_STORAGE_BYTES;
             address += FLASH_SECTOR_BYTES) {
            if (!eraseInternalFlashPage(address)) {
                failure = StorageError::FormatFailed;
                return false;
            }
        }

        StorageHeader header = {SURVEY_STORAGE_MAGIC, SURVEY_FORMAT_VERSION, sizeof(SurveyRecord),
                                SURVEY_STORAGE_BYTES, SURVEY_FREQUENCY_HZ, 0};
        header.crc32 = crc32(&header, offsetof(StorageHeader, crc32));
        if (!writeInternalFlash(partitionStart, &header, sizeof(header))) {
            failure = StorageError::FormatFailed;
            return false;
        }
        activeVersion = SURVEY_FORMAT_VERSION;
        activeRecordSize = sizeof(SurveyRecord);
        appendEnabled = true;
        formatRequired = false;
        failure = StorageError::None;
        slotCount = 0;
        return true;
    }

    bool append(SurveyRecord &record)
    {
        if (!appendEnabled || slotCount >= capacity())
            return false;
        record.magic = SURVEY_RECORD_MAGIC;
        record.version = SURVEY_FORMAT_VERSION;
        record.crc32 = crc32(&record, offsetof(SurveyRecord, crc32));
        const uint32_t address = recordAddress(slotCount);
        if (!writeInternalFlash(address, &record, sizeof(SurveyRecord))) {
            failure = StorageError::WriteFailed;
            return false;
        }
        ++slotCount;
        return true;
    }

    uint32_t slots() const { return slotCount; }
    uint8_t version() const { return activeVersion; }
    uint16_t recordSize() const { return activeRecordSize; }
    bool canAppend() const { return appendEnabled; }
    bool needsFormat() const { return formatRequired; }
    StorageError error() const { return failure; }
    uint32_t flashBytes() const { return SURVEY_STORAGE_BYTES; }
    uint32_t usedBytes() const { return slotCount * activeRecordSize; }
    uint32_t freeRecords() const { return slotCount <= capacity() ? capacity() - slotCount : 0; }

    const char *errorText() const
    {
        switch (failure) {
        case StorageError::LayoutInvalid:
            return "FLASH LAYOUT ERROR";
        case StorageError::FormatFailed:
            return "FLASH FORMAT FAILED";
        case StorageError::WriteFailed:
            return "FLASH WRITE FAILED";
        default:
            return "Storage unavailable";
        }
    }

    uint32_t capacity() const
    {
        const uint16_t size = activeRecordSize ? activeRecordSize : sizeof(SurveyRecord);
        return (SURVEY_STORAGE_BYTES - SURVEY_STORAGE_HEADER_BYTES) / size;
    }

    bool hasSpace(uint32_t records) const
    {
        return appendEnabled && slotCount <= capacity() && records <= capacity() - slotCount;
    }

    bool readRaw(uint32_t slot, uint8_t *record)
    {
        if (slot >= slotCount || !activeRecordSize)
            return false;
        readInternalFlash(recordAddress(slot), record, activeRecordSize);
        return true;
    }

  private:
    uint32_t partitionStart = 0;
    uint32_t slotCount = 0;
    uint16_t activeRecordSize = 0;
    uint8_t activeVersion = 0;
    bool appendEnabled = false;
    bool formatRequired = false;
    StorageError failure = StorageError::None;

    uint32_t recordAddress(uint32_t slot) const
    {
        return partitionStart + SURVEY_STORAGE_HEADER_BYTES + slot * activeRecordSize;
    }

    void scan()
    {
        uint8_t record[sizeof(LegacySurveyRecordV1)] = {};
        slotCount = 0;
        while (slotCount < capacity()) {
            readInternalFlash(recordAddress(slotCount), record, activeRecordSize);
            if (allErased(record, activeRecordSize))
                break;
            ++slotCount;
        }
    }
};

SurveyStorage storage;

void redrawMenuOverlay();
bool gpsSpeedIsFresh();
uint8_t requiredGpsGoodFixes();

void onRadioInterrupt()
{
    radioInterrupt = true;
}

void onButtonInterrupt()
{
    const uint8_t head = buttonEdgeHead;
    const uint8_t next = static_cast<uint8_t>((head + 1) % BUTTON_EDGE_QUEUE_CAPACITY);
    if (next == buttonEdgeTail)
        return;
    buttonEdgeTimes[head] = millis();
    buttonEdgeLevels[head] = digitalRead(USER_BUTTON_PIN) == LOW ? 1 : 0;
    buttonEdgeHead = next;
}

size_t wrappedLineCount(const char *text, size_t columns)
{
    if (!text || !*text || !columns)
        return 0;
    size_t lines = 0;
    const char *cursor = text;
    while (*cursor) {
        while (*cursor == ' ')
            ++cursor;
        if (!*cursor)
            break;
        const size_t remaining = strlen(cursor);
        size_t take = remaining < columns ? remaining : columns;
        const char *newline = static_cast<const char *>(memchr(cursor, '\n', take));
        if (newline) {
            take = static_cast<size_t>(newline - cursor);
        } else if (remaining > columns) {
            size_t breakAt = take;
            while (breakAt > 0 && cursor[breakAt] != ' ')
                --breakAt;
            if (breakAt > 0)
                take = breakAt;
        }
        if (!take)
            take = 1;
        cursor += take;
        if (*cursor == '\n')
            ++cursor;
        while (*cursor == ' ')
            ++cursor;
        ++lines;
    }
    return lines;
}

int16_t drawWrappedText(const char *text, int16_t x, int16_t y, int16_t width, int16_t bottom,
                        uint8_t textSize)
{
    if (!text || !*text || width <= 0 || textSize == 0)
        return y;
    const size_t columns = static_cast<size_t>(width / (6 * textSize));
    const int16_t lineHeight = static_cast<int16_t>(8 * textSize + 2);
    const char *cursor = text;
    display.setTextSize(textSize);
    display.setTextWrap(false);
    while (*cursor && y + static_cast<int16_t>(8 * textSize) <= bottom) {
        while (*cursor == ' ')
            ++cursor;
        if (!*cursor)
            break;
        const size_t remaining = strlen(cursor);
        size_t take = remaining < columns ? remaining : columns;
        const char *newline = static_cast<const char *>(memchr(cursor, '\n', take));
        if (newline) {
            take = static_cast<size_t>(newline - cursor);
        } else if (remaining > columns) {
            size_t breakAt = take;
            while (breakAt > 0 && cursor[breakAt] != ' ')
                --breakAt;
            if (breakAt > 0)
                take = breakAt;
        }
        if (!take)
            take = 1;
        display.setCursor(x, y);
        for (size_t index = 0; index < take; ++index)
            display.write(cursor[index]);
        cursor += take;
        if (*cursor == '\n')
            ++cursor;
        while (*cursor == ' ')
            ++cursor;
        y += lineHeight;
    }
    return y;
}

void drawStatusText(const char *text, int16_t y, int16_t bottom, uint8_t preferredSize)
{
    if (!text)
        return;
    constexpr int16_t x = 4;
    constexpr int16_t width = 232;
    const size_t preferredColumns = static_cast<size_t>(width / (6 * preferredSize));
    const uint8_t size = wrappedLineCount(text, preferredColumns) == 1 ? preferredSize : 1;
    drawWrappedText(text, x, y, width, bottom, size);
}

uint16_t statusTitleColor(const char *title)
{
    if (!title)
        return ST77XX_WHITE;
    if (strstr(title, "FAILED") || strstr(title, "FAIL") || strstr(title, "FULL") ||
        strstr(title, "TIMEOUT") || strstr(title, "DRIFT REJECTED"))
        return ST77XX_RED;
    if (strstr(title, "SENT") || strstr(title, "RECEIVED") || strstr(title, "GPS LOCK") ||
        strstr(title, "LOGGING ACTIVE") || strstr(title, "LOGS WIPED"))
        return ST77XX_GREEN;
    if (strstr(title, "WAITING") || strstr(title, "PAUSED") || strstr(title, "QUALITY LOW") ||
        strstr(title, "STABILIZING") || strstr(title, "NEEDS WIPE") || strstr(title, "WIPING"))
        return ST77XX_YELLOW;
    return ST77XX_YELLOW;
}

void drawBatteryStatus()
{
    constexpr int16_t batteryX = 174;
    constexpr int16_t batteryY = 121;
    constexpr int16_t batteryWidth = 19;
    constexpr int16_t batteryHeight = 10;
    display.fillRect(172, 120, 68, 15, ST77XX_BLACK);
    display.drawRect(batteryX, batteryY, batteryWidth, batteryHeight, ST77XX_WHITE);
    display.fillRect(batteryX + batteryWidth, batteryY + 3, 2, 4, ST77XX_WHITE);
    display.setTextSize(1);
    display.setCursor(200, 122);
    if (batteryReadingValid) {
        const uint8_t percent = batteryPercent(batteryMillivolts);
        const uint16_t color = percent > 50 ? ST77XX_GREEN : (percent > 20 ? ST77XX_YELLOW : ST77XX_RED);
        const int16_t fillWidth = static_cast<int16_t>((batteryWidth - 4) * percent / 100);
        if (fillWidth > 0)
            display.fillRect(batteryX + 2, batteryY + 2, fillWidth, batteryHeight - 4, color);
        display.setTextColor(color);
        display.print(percent);
        display.print('%');
    } else {
        display.setTextColor(ST77XX_WHITE);
        display.print("--");
    }
}

const char *dashboardQuality(const DashboardRow &row)
{
    if (!row.signalValid)
        return "--";
    if (row.rssi >= -100 && row.snrCenti >= 0)
        return "GOOD";
    if (row.rssi >= -115 && row.snrCenti >= -1000)
        return "FAIR";
    return "WEAK";
}

uint16_t dashboardQualityColor(const DashboardRow &row)
{
    if (!row.signalValid)
        return ST77XX_WHITE;
    if (row.rssi >= -100 && row.snrCenti >= 0)
        return ST77XX_GREEN;
    if (row.rssi >= -115 && row.snrCenti >= -1000)
        return ST77XX_YELLOW;
    return ST77XX_RED;
}

const char *dashboardResponseText(DashboardResponse response)
{
    switch (response) {
    case DashboardResponse::Waiting:
        return "WAIT";
    case DashboardResponse::Received:
        return "YES";
    case DashboardResponse::Missed:
        return "NO";
    case DashboardResponse::ReplySent:
        return "SENT";
    case DashboardResponse::ReplyFailed:
        return "FAIL";
    case DashboardResponse::SendFailed:
        return "TX ERR";
    }
    return "--";
}

uint16_t dashboardResponseColor(DashboardResponse response)
{
    if (response == DashboardResponse::Waiting)
        return ST77XX_YELLOW;
    if (response == DashboardResponse::Received || response == DashboardResponse::ReplySent)
        return ST77XX_GREEN;
    return ST77XX_RED;
}

void drawDashboardHeader()
{
    display.fillRect(0, 0, 240, 15, ST77XX_BLACK);
    display.setTextWrap(false);
    display.setTextSize(1);
    display.setTextColor(ST77XX_CYAN);
    display.setCursor(4, 4);
    display.print(ROLE_NAME);
    display.print(" LOG");
    display.fillRect(100, 0, 140, 14, ST77XX_BLACK);
    display.setTextColor(loggingEnabled ? dashboardStatusColor : ST77XX_CYAN);
    display.setCursor(104, 4);
    if (loggingEnabled) {
        display.print(dashboardStatus);
    } else if (storageReady) {
        display.print(storage.slots());
        display.print(" SAVED");
    } else {
        display.print("STORAGE --");
    }
    display.drawFastHLine(0, 14, 240, ST77XX_WHITE);
}

void drawDashboardRows()
{
    display.fillRect(0, 15, 240, 105, ST77XX_BLACK);
    display.setTextSize(1);
    display.setTextColor(ST77XX_CYAN);
    display.setCursor(4, 18);
    display.print("PING");
    display.setCursor(45, 18);
    display.print("QUALITY");
    display.setCursor(92, 18);
    display.print("RSSI/SNR");
    display.setCursor(184, 18);
    display.print("RESPONSE");
    display.drawFastHLine(0, 28, 240, display.color565(70, 70, 70));

    for (uint8_t index = 0; index < dashboardRowCount; ++index) {
        const DashboardRow &row = dashboardRows[index];
        const int16_t y = 32 + index * 10;
        display.setTextColor(ST77XX_WHITE);
        display.setCursor(4, y);
        display.print('#');
        display.print(row.sequence);
        display.setTextColor(dashboardQualityColor(row));
        display.setCursor(45, y);
        display.print(dashboardQuality(row));
        display.setCursor(92, y);
        if (row.signalValid) {
            display.print(row.rssi);
            display.print('/');
            display.print(row.snrCenti / 100.0F, 1);
        } else {
            display.print("--");
        }
        display.setTextColor(dashboardResponseColor(row.response));
        display.setCursor(184, y);
        display.print(dashboardResponseText(row.response));
    }
}

bool dashboardRowFromStoredFields(uint8_t roleValue, uint8_t eventValue, uint8_t flags, uint32_t sequence,
                                  int16_t localRssi, int16_t localSnrCenti, int16_t remoteRssi,
                                  int16_t remoteSnrCenti, DashboardRow &row)
{
    const SurveyRole storedRole = static_cast<SurveyRole>(roleValue);
    const SurveyEvent event = static_cast<SurveyEvent>(eventValue);
    row = {true, false, sequence, 0, 0, DashboardResponse::Missed};

    if (storedRole == SurveyRole::Mobile) {
        if (event == SurveyEvent::ReplyRx) {
            if (flags & RemoteRxValid) {
                row.signalValid = true;
                row.rssi = remoteRssi;
                row.snrCenti = remoteSnrCenti;
            } else if (flags & LocalRxValid) {
                row.signalValid = true;
                row.rssi = localRssi;
                row.snrCenti = localSnrCenti;
            }
            row.response = DashboardResponse::Received;
            return true;
        }
        if (event == SurveyEvent::Timeout) {
            row.response = DashboardResponse::Missed;
            return true;
        }
        if (event == SurveyEvent::Send) {
            row.response = DashboardResponse::Waiting;
            return true;
        }
        return false;
    }

    if (storedRole == SurveyRole::Base && event == SurveyEvent::ProbeRx) {
        row.signalValid = (flags & LocalRxValid) != 0;
        row.rssi = localRssi;
        row.snrCenti = localSnrCenti;
        row.response = flags & ReplySent ? DashboardResponse::ReplySent : DashboardResponse::ReplyFailed;
        return true;
    }
    if (storedRole == SurveyRole::Base && event == SurveyEvent::ReplyTx) {
        row.response = DashboardResponse::ReplySent;
        return true;
    }
    return false;
}

bool readStoredDashboardRow(uint32_t slot, DashboardRow &row, uint32_t &sessionIdValue)
{
    uint8_t raw[sizeof(LegacySurveyRecordV1)] = {};
    if (!storage.readRaw(slot, raw))
        return false;

    if (storage.version() == SURVEY_FORMAT_VERSION && storage.recordSize() == sizeof(SurveyRecord)) {
        SurveyRecord record = {};
        memcpy(&record, raw, sizeof(record));
        if (record.magic != SURVEY_RECORD_MAGIC || record.version != SURVEY_FORMAT_VERSION ||
            record.crc32 != crc32(&record, offsetof(SurveyRecord, crc32)))
            return false;
        sessionIdValue = record.sessionId;
        return dashboardRowFromStoredFields(record.role, record.event, record.flags, record.sequence,
                                            record.localRssiDbm, record.localSnrCentiDb, record.remoteRssiDbm,
                                            record.remoteSnrCentiDb, row);
    }

    if (storage.version() == SURVEY_LEGACY_FORMAT_VERSION &&
        storage.recordSize() == sizeof(LegacySurveyRecordV1)) {
        LegacySurveyRecordV1 record = {};
        memcpy(&record, raw, sizeof(record));
        if (record.magic != SURVEY_RECORD_MAGIC || record.version != SURVEY_LEGACY_FORMAT_VERSION ||
            record.crc32 != crc32(&record, offsetof(LegacySurveyRecordV1, crc32)))
            return false;
        sessionIdValue = record.sessionId;
        return dashboardRowFromStoredFields(record.role, record.event, record.flags, record.sequence,
                                            record.localRssiDbm, record.localSnrCentiDb, record.remoteRssiDbm,
                                            record.remoteSnrCentiDb, row);
    }
    return false;
}

void loadStoredDashboardRows()
{
    DashboardRow newest[DASHBOARD_ROW_COUNT] = {};
    uint32_t sessions[DASHBOARD_ROW_COUNT] = {};
    uint8_t count = 0;
    for (uint32_t slot = storage.slots(); slot > 0 && count < DASHBOARD_ROW_COUNT;) {
        --slot;
        DashboardRow row = {};
        uint32_t storedSession = 0;
        if (!readStoredDashboardRow(slot, row, storedSession))
            continue;
        bool duplicate = false;
        for (uint8_t index = 0; index < count; ++index) {
            if (sessions[index] == storedSession && newest[index].sequence == row.sequence) {
                duplicate = true;
                break;
            }
        }
        if (duplicate)
            continue;
        newest[count] = row;
        sessions[count] = storedSession;
        ++count;
    }

    memset(dashboardRows, 0, sizeof(dashboardRows));
    dashboardRowCount = count;
    for (uint8_t index = 0; index < count; ++index)
        dashboardRows[index] = newest[count - 1 - index];
}

void drawDashboardFooter()
{
    display.fillRect(0, 120, 172, 15, ST77XX_BLACK);
    display.setTextSize(1);
    display.setTextColor(ST77XX_WHITE);
    display.setCursor(4, 122);
    display.print(ROLE_NAME);
    display.print(loggingEnabled ? " R" : " P");
    display.print(" L");
    if (storageReady) {
        display.print(storage.slots());
        display.print('/');
        display.print(storage.capacity());
    } else {
        display.print('-');
    }
    display.print(" T:CTRL");
    drawBatteryStatus();
}

void drawDashboard()
{
    controlCenterSelected = false;
    controlCenterRendered = false;
    transientNoticeUntilMs = 0;
    updateBatteryReading();
    if (!loggingEnabled && storageReady)
        loadStoredDashboardRows();
    display.fillScreen(ST77XX_BLACK);
    drawDashboardHeader();
    drawDashboardRows();
    drawDashboardFooter();
    if (menuMode != MenuMode::Closed)
        redrawMenuOverlay();
}

void drawPausedPanel(int16_t x, int16_t width, const char *title)
{
    const uint16_t panel = display.color565(7, 18, 27);
    const uint16_t border = display.color565(24, 82, 105);
    display.fillRoundRect(x, 22, width, 72, 5, panel);
    display.drawRoundRect(x, 22, width, 72, 5, border);
    display.setTextSize(1);
    display.setTextColor(ST77XX_CYAN);
    display.setCursor(x + 6, 27);
    display.print(title);
}

void drawPausedGpsLine(uint8_t index, const char *text, uint16_t color, int16_t y, uint8_t textSize,
                       int16_t clearY, int16_t clearHeight)
{
    if (pausedGpsCacheValid && !strcmp(pausedGpsRendered[index], text) &&
        pausedGpsRenderedColor[index] == color)
        return;
    strncpy(pausedGpsRendered[index], text, sizeof(pausedGpsRendered[index]) - 1);
    pausedGpsRendered[index][sizeof(pausedGpsRendered[index]) - 1] = '\0';
    pausedGpsRenderedColor[index] = color;
    display.fillRect(7, clearY, 107, clearHeight, display.color565(7, 18, 27));
    display.setTextSize(textSize);
    display.setTextColor(color);
    display.setCursor(10, y);
    display.print(text);
}

void drawPausedGpsMetrics()
{
    const bool gpsFresh = gps.location.isValid() && gps.location.age() < GPS_MAX_FIX_AGE_MS;
    const bool gpsLocked = gpsFixTrusted && gpsFresh;
    const bool noData = gps.charsProcessed() < 10;
    const char *status = gpsLocked ? "LOCKED" : (noData ? "NO DATA" : "SEARCH");
    const uint16_t statusColor = gpsLocked ? ST77XX_GREEN : (noData ? ST77XX_RED : ST77XX_YELLOW);

    char satellites[5] = "--";
    char hdop[8] = "--";
    char speed[8] = "--";
    if (gps.satellites.isValid())
        snprintf(satellites, sizeof(satellites), "%02lu", gps.satellites.value());
    if (gps.hdop.isValid())
        snprintf(hdop, sizeof(hdop), "%.1f", gps.hdop.hdop());
    if (gpsSpeedIsFresh())
        snprintf(speed, sizeof(speed), "%.1f", gps.speed.kmph());

    char quality[24];
    char position1[24];
    char position2[24];
    snprintf(quality, sizeof(quality), "S%s H%s V%s", satellites, hdop, speed);
    if (gpsLocked) {
        snprintf(position1, sizeof(position1), "LAT %.5f", gps.location.lat());
        snprintf(position2, sizeof(position2), "LON %.5f", gps.location.lng());
    } else {
        snprintf(position1, sizeof(position1), "FIX %u/%u", gpsGoodFixCount, requiredGpsGoodFixes());
        snprintf(position2, sizeof(position2), "GPS quality pending");
    }

    drawPausedGpsLine(0, status, statusColor, 39, 2, 37, 19);
    drawPausedGpsLine(1, quality, ST77XX_WHITE, 59, 1, 57, 10);
    drawPausedGpsLine(2, position1, ST77XX_WHITE, 70, 1, 68, 10);
    drawPausedGpsLine(3, position2, ST77XX_WHITE, 81, 1, 79, 10);
    pausedGpsCacheValid = true;
}

void drawControlStorageMetrics()
{
    const uint32_t slots = storageReady ? storage.slots() : 0;
    const bool soundOn = ROLE == SurveyRole::Mobile && soundEnabled;
    if (controlStorageCacheValid && controlStorageReadyRendered == storageReady &&
        controlStorageSlotsRendered == slots && controlSoundRendered == soundOn &&
        controlScreenTimeoutRendered == screenTimeoutSelection)
        return;
    controlStorageReadyRendered = storageReady;
    controlStorageSlotsRendered = slots;
    controlSoundRendered = soundOn;
    controlScreenTimeoutRendered = screenTimeoutSelection;

    const uint16_t panel = display.color565(7, 18, 27);
    display.fillRect(125, 37, 108, 19, panel);
    display.setTextSize(2);
    display.setTextColor(ST77XX_GREEN);
    display.setCursor(128, 39);
    if (storageReady)
        display.print(slots);
    else
        display.print("--");
    display.setTextSize(1);
    display.print(" REC");

    constexpr int16_t storageBarX = 128;
    constexpr int16_t storageBarY = 57;
    constexpr int16_t storageBarWidth = 102;
    display.drawRect(storageBarX, storageBarY, storageBarWidth, 6, ST77XX_WHITE);
    display.fillRect(storageBarX + 1, storageBarY + 1, storageBarWidth - 2, 4, panel);
    if (storageReady && storage.capacity()) {
        const uint32_t fill = (storageBarWidth - 2) * slots / storage.capacity();
        if (fill)
            display.fillRect(storageBarX + 1, storageBarY + 1, fill, 4, ST77XX_CYAN);
    }

    display.fillRect(125, 64, 108, 29, panel);
    display.setTextSize(1);
    display.setTextColor(ST77XX_WHITE);
    display.setCursor(128, 66);
    display.print("FREE ");
    if (storageReady)
        display.print(storage.freeRecords());
    else
        display.print("--");
    display.setCursor(128, 76);
    display.print("DATA ");
    if (storageReady) {
        display.print(storage.usedBytes() / 1024UL);
        display.print('/');
        display.print((SURVEY_STORAGE_BYTES - SURVEY_STORAGE_HEADER_BYTES) / 1024UL);
        display.print(" KB");
    } else {
        display.print("--");
    }
    display.setCursor(128, 86);
    if (ROLE == SurveyRole::Mobile) {
        display.print("SND ");
        display.setTextColor(soundEnabled ? ST77XX_GREEN : ST77XX_YELLOW);
        display.print(soundEnabled ? "ON" : "MUTED");
    } else {
        display.print("SND --");
    }
    display.setTextColor(ST77XX_WHITE);
    display.print(" SCR ");
    display.print(screenTimeoutShortLabel());
    controlStorageCacheValid = true;
}

void drawControlCenterHeader()
{
    const uint16_t header = display.color565(5, 28, 42);
    display.fillRect(0, 0, 240, 18, header);
    display.fillRect(0, 0, 4, 18, ST77XX_CYAN);
    display.setTextSize(1);
    display.setTextColor(ST77XX_WHITE);
    display.setCursor(9, 5);
    display.print(ROLE_NAME);
    display.print(" // CONTROL CENTER");
    const uint16_t modeColor = loggingEnabled ? ST77XX_GREEN : ST77XX_YELLOW;
    display.drawRoundRect(185, 2, 51, 13, 4, modeColor);
    display.setTextColor(modeColor);
    display.setCursor(192, 5);
    display.print(loggingEnabled ? "ACTIVE" : "PAUSED");
}

void drawControlCenter()
{
    controlCenterSelected = true;
    controlCenterRendered = true;
    transientNoticeUntilMs = 0;
    updateBatteryReading();
    display.fillScreen(ST77XX_BLACK);
    display.setTextWrap(false);
    drawControlCenterHeader();

    drawPausedPanel(4, 113, "GPS STATUS");
    drawPausedPanel(122, 114, "LOG STORAGE");
    pausedGpsCacheValid = false;
    drawPausedGpsMetrics();
    controlStorageCacheValid = false;
    drawControlStorageMetrics();

    const uint16_t radioPanel = display.color565(7, 18, 27);
    display.fillRoundRect(4, 97, 232, 20, 4, radioPanel);
    display.drawRoundRect(4, 97, 232, 20, 4, display.color565(24, 82, 105));
    display.setTextColor(radioReady ? ST77XX_GREEN : ST77XX_RED);
    display.setCursor(9, 100);
    display.print(radioReady ? "RF" : "RF!");
    display.setTextColor(ST77XX_WHITE);
    display.print(' ');
    display.print(SURVEY_FREQUENCY_MHZ, 3);
    display.print("MHz SF");
    display.print(SURVEY_SPREADING_FACTOR);
    display.print(" CR4/");
    display.print(SURVEY_CODING_RATE);
    display.setCursor(9, 109);
    display.print("BW");
    display.print(static_cast<unsigned>(SURVEY_BANDWIDTH_KHZ));
    display.print(" TX");
    display.print(SURVEY_TX_POWER_DBM);
    display.print("dBm ID ");
    char shortId[9];
    snprintf(shortId, sizeof(shortId), "%08lX", static_cast<unsigned long>(deviceId));
    display.print(shortId);

    display.fillRect(0, 120, 172, 15, ST77XX_BLACK);
    display.setTextColor(ST77XX_CYAN);
    display.setCursor(4, 122);
    display.print("TAP: LOG  HOLD: MENU");
    drawBatteryStatus();
    if (menuMode != MenuMode::Closed)
        redrawMenuOverlay();
}

void showLogsWipedNotice()
{
    const uint16_t notice = display.color565(0, 92, 62);
    display.fillRect(0, 0, 240, 18, notice);
    display.fillRect(0, 0, 4, 18, ST77XX_GREEN);
    display.setTextWrap(false);
    display.setTextSize(1);
    display.setTextColor(ST77XX_WHITE);
    display.setCursor(9, 5);
    display.print("LOGS WIPED // 0 RECORDS");
    transientNoticeUntilMs = millis() + 2500;
}

void serviceTransientNotice()
{
    if (!transientNoticeUntilMs || static_cast<int32_t>(millis() - transientNoticeUntilMs) < 0)
        return;
    transientNoticeUntilMs = 0;
    if (controlCenterRendered && menuMode == MenuMode::Closed)
        drawControlCenterHeader();
}

void setDashboardStatus(const char *status, uint16_t color)
{
    strncpy(dashboardStatus, status, sizeof(dashboardStatus) - 1);
    dashboardStatus[sizeof(dashboardStatus) - 1] = '\0';
    dashboardStatusColor = color;
    if (loggingEnabled && !controlCenterSelected && menuMode == MenuMode::Closed)
        drawDashboardHeader();
}

void clearDashboardRows()
{
    memset(dashboardRows, 0, sizeof(dashboardRows));
    dashboardRowCount = 0;
}

void addDashboardRow(uint32_t sequence, bool signalValid, int16_t rssi, int16_t snrCenti,
                     DashboardResponse response)
{
    if (dashboardRowCount == DASHBOARD_ROW_COUNT) {
        memmove(&dashboardRows[0], &dashboardRows[1],
                sizeof(DashboardRow) * (DASHBOARD_ROW_COUNT - 1));
        --dashboardRowCount;
    }
    dashboardRows[dashboardRowCount++] = {true, signalValid, sequence, rssi, snrCenti, response};
    if (loggingEnabled && menuMode == MenuMode::Closed) {
        if (controlCenterSelected) {
            drawControlStorageMetrics();
        } else {
            drawDashboardRows();
            drawDashboardFooter();
        }
    }
}

void updateDashboardRow(uint32_t sequence, bool signalValid, int16_t rssi, int16_t snrCenti,
                        DashboardResponse response)
{
    for (int8_t index = static_cast<int8_t>(dashboardRowCount) - 1; index >= 0; --index) {
        DashboardRow &row = dashboardRows[index];
        if (row.active && row.sequence == sequence) {
            row.signalValid = signalValid;
            row.rssi = rssi;
            row.snrCenti = snrCenti;
            row.response = response;
            if (loggingEnabled && menuMode == MenuMode::Closed) {
                if (controlCenterSelected) {
                    drawControlStorageMetrics();
                } else {
                    drawDashboardRows();
                    drawDashboardFooter();
                }
            }
            return;
        }
    }
    addDashboardRow(sequence, signalValid, rssi, snrCenti, response);
}

void showScreen(const char *title, const char *line1 = nullptr, const char *line2 = nullptr, const char *line3 = nullptr)
{
    controlCenterRendered = false;
    transientNoticeUntilMs = 0;
    updateBatteryReading();
    display.fillScreen(ST77XX_BLACK);
    display.setTextWrap(false);
    display.setTextColor(statusTitleColor(title));
    drawStatusText(title, 4, 21, 2);
    display.drawFastHLine(0, 24, 240, ST77XX_WHITE);
    display.setTextColor(ST77XX_WHITE);
    drawStatusText(line1, 32, 58, 2);
    drawStatusText(line2, 62, 98, 2);
    drawStatusText(line3, 102, 118, 1);
    display.setTextSize(1);
    display.setCursor(4, 122);
    display.print(ROLE_NAME);
    display.print(loggingEnabled ? " R" : " P");
    display.print(" L");
    if (storageReady) {
        display.print(storage.slots());
        display.print('/');
        display.print(storage.capacity());
    } else {
        display.print('-');
    }
    drawBatteryStatus();
    if (menuMode != MenuMode::Closed)
        redrawMenuOverlay();
}

#if defined(SURVEY_ROLE_MOBILE)
void showMovementWaiting(double distanceMeters, float requiredDistanceMeters)
{
    (void)distanceMeters;
    (void)requiredDistanceMeters;
    setDashboardStatus("WAIT MOVEMENT", ST77XX_YELLOW);
}
#endif

SurveyPosition currentPosition();
bool appendEvent(SurveyEvent event, uint32_t sequence, uint64_t peer, const SurveyPosition &local,
                 const SurveyPosition &remote, bool localRxValid = false, int16_t localRssi = 0,
                 int16_t localSnrCenti = 0, bool remoteRxValid = false, int16_t remoteRssi = 0,
                 int16_t remoteSnrCenti = 0, uint32_t packetId = 0, uint8_t extraFlags = 0,
                 uint32_t eventSessionId = 0);

void clearPendingProbes()
{
    for (auto &probe : pending)
        probe.active = false;
}

void showLoggingStatus()
{
    if (!storageReady)
        showScreen("STORAGE FAILED", storage.errorText(), "Data cannot persist");
    else if (loggingEnabled) {
        if (controlCenterSelected)
            drawControlCenter();
        else
            drawDashboard();
    }
    else if (storage.needsFormat())
        showScreen("LOG NEEDS WIPE", "Old/unknown format", "Extract first if needed");
    else if (!storage.canAppend())
        showScreen("OLD LOG FOUND", "Extraction still works", "Wipe to use compact log");
    else if (controlCenterSelected)
        drawControlCenter();
    else
        drawDashboard();
}

const char *menuLabel(uint8_t selection)
{
    switch (selection) {
    case 0:
        return loggingEnabled ? "STOP LOGGING" : "START / APPEND";
    case 1:
        if (ROLE == SurveyRole::Base)
            return "SOUND: BASE SILENT";
        return soundEnabled ? "SOUND: ON" : "SOUND: MUTED";
    case 2:
        return screenTimeoutMenuLabel();
    case 3:
        return "WIPE ALL LOGS";
    case 4:
        return "RESTART DEVICE";
    case 5:
        return "POWER OFF";
    default:
        return "EXIT MENU";
    }
}

uint16_t popupAccent()
{
    return ST77XX_YELLOW;
}

uint16_t popupPanelColor()
{
    return display.color565(18, 22, 30);
}

void drawPopupFrame(const char *title)
{
    constexpr int16_t x = 8;
    constexpr int16_t y = 4;
    constexpr int16_t width = 224;
    constexpr int16_t height = 126;
    const uint16_t panel = popupPanelColor();
    display.fillRoundRect(x, y, width, height, 7, ST77XX_BLACK);
    display.fillRoundRect(x + 2, y + 2, width - 4, height - 4, 5, panel);
    display.drawRoundRect(x, y, width, height, 7, popupAccent());
    display.setTextWrap(false);
    display.setTextSize(1);
    display.setTextColor(popupAccent());
    display.setCursor(18, 11);
    display.print(title);
    display.drawFastHLine(16, 23, 208, ST77XX_WHITE);
}

void drawMenuOption(uint8_t option, bool selected)
{
    const int16_t y = 27 + option * 13;
    display.fillRect(15, y - 2, 210, 11, popupPanelColor());
    if (selected) {
        display.fillRoundRect(15, y - 2, 210, 11, 3, popupAccent());
        display.setTextColor(ST77XX_BLACK);
    } else {
        display.setTextColor(ST77XX_WHITE);
    }
    display.setTextSize(1);
    display.setCursor(20, y);
    display.print(selected ? "> " : "  ");
    display.print(menuLabel(option));
}

void clearMenuHoldProgress()
{
    display.fillRect(18, 119, 204, 4, popupPanelColor());
}

void showMenu()
{
    drawPopupFrame("SURVEY MENU");
    for (uint8_t option = 0; option < MENU_OPTION_COUNT; ++option)
        drawMenuOption(option, option == menuSelection);
}

void showConfirmationPopup(const char *title, const char *warning, const char *confirmText)
{
    drawPopupFrame(title);
    display.setTextColor(ST77XX_WHITE);
    drawWrappedText(warning, 20, 38, 200, 68, 2);
    display.setTextColor(ST77XX_YELLOW);
    drawWrappedText(confirmText, 20, 76, 200, 96, 1);
    display.setTextColor(ST77XX_WHITE);
    display.setCursor(20, 101);
    display.print("Tap: cancel");
}

void redrawMenuOverlay()
{
    if (menuMode == MenuMode::Select)
        showMenu();
    else if (menuMode == MenuMode::ConfirmErase)
        showConfirmationPopup("CONFIRM WIPE", "DELETE ALL LOGS", "Hold again to erase");
    else if (menuMode == MenuMode::ConfirmPowerOff)
        showConfirmationPopup("CONFIRM POWER OFF", "TURN DEVICE OFF", "Hold again to power off");
}

void startLogging()
{
    if (!storageReady) {
        showScreen("CANNOT START", "Storage unavailable");
        return;
    }
    if (!storage.canAppend()) {
        showScreen("CANNOT APPEND", storage.needsFormat() ? "Unknown log format" : "Old log is read-only",
                   "Extract or wipe it");
        return;
    }
    if (!storage.hasSpace(1)) {
        showScreen("LOG STORAGE FULL", "Extract or wipe logs");
        return;
    }
    sessionId = static_cast<uint32_t>(random(1, INT32_MAX));
    clearPendingProbes();
    clearDashboardRows();
    controlCenterSelected = false;
#if defined(SURVEY_ROLE_MOBILE)
    nextSequence = 0;
    lastSendMs = 0;
    lastProbePositionValid = false;
#endif
    loggingEnabled = true;
    setDashboardStatus(ROLE == SurveyRole::Mobile ? "GPS WAIT" : "LISTENING", ST77XX_YELLOW);
    if (!appendEvent(SurveyEvent::Boot, 0, 0, currentPosition(), {})) {
        loggingEnabled = false;
        showScreen("LOG WRITE FAILED", "Logging remains paused", "Restart or extract logs");
        return;
    }
    showLoggingStatus();
}

void stopLogging()
{
    loggingEnabled = false;
    controlCenterSelected = true;
    clearPendingProbes();
    showLoggingStatus();
}

void wipeLogs()
{
    loggingEnabled = false;
    clearPendingProbes();
    showScreen("WIPING LOGS", "Please wait...", "Do not remove power");
    if (storageReady && storage.format()) {
        drawControlCenter();
        showLogsWipedNotice();
    } else {
        showScreen("WIPE FAILED", "Storage unavailable");
    }
}

[[noreturn]] void powerOffDevice()
{
    loggingEnabled = false;
    clearPendingProbes();
    showScreen("POWER OFF ACCEPTED", "Release button", "Then press to wake");
    while (digitalRead(USER_BUTTON_PIN) == LOW)
        delay(5);
    delay(50);
#if defined(SURVEY_ROLE_MOBILE)
    noTone(BUZZER_PIN);
#endif
    if (radioReady) {
        radio.clearDio1Action();
        radio.sleep();
    }
    digitalWrite(GPS_STANDBY, LOW);
    digitalWrite(PERIPHERAL_POWER, LOW);
    digitalWrite(TFT_BACKLIGHT, HIGH);
    digitalWrite(TFT_POWER, HIGH);
    const uint32_t physicalPin = g_ADigitalPinMap[USER_BUTTON_PIN];
    nrf_gpio_cfg_sense_input(physicalPin, NRF_GPIO_PIN_PULLUP, NRF_GPIO_PIN_SENSE_LOW);
    // System-off wake is a reset while the wake/DFU button is still held.
    // Ask the bootloader to skip its button check once and start this app.
    NRF_POWER->GPREGRET = BOOTLOADER_SKIP_DFU_MAGIC;
    NRF_POWER->SYSTEMOFF = 1;
    __DSB();
    while (true)
        __WFE();
}

void handleShortButtonPress()
{
    if (menuMode == MenuMode::Closed) {
        if (controlCenterRendered)
            drawDashboard();
        else
            drawControlCenter();
        return;
    }
    lastMenuInteractionMs = millis();
    if (menuMode == MenuMode::Select) {
        const uint8_t previousSelection = menuSelection;
        menuSelection = (menuSelection + 1) % MENU_OPTION_COUNT;
        clearMenuHoldProgress();
        drawMenuOption(previousSelection, false);
        drawMenuOption(menuSelection, true);
    } else {
        menuMode = MenuMode::Select;
        showMenu();
    }
}

void handleLongButtonPress()
{
    lastMenuInteractionMs = millis();
    if (menuMode == MenuMode::Closed) {
        menuMode = MenuMode::Select;
        menuSelection = 0;
        showMenu();
        return;
    }
    if (menuMode == MenuMode::ConfirmErase) {
        menuMode = MenuMode::Closed;
        wipeLogs();
        return;
    }
    if (menuMode == MenuMode::ConfirmPowerOff) {
        menuMode = MenuMode::Closed;
        powerOffDevice();
    }
    switch (menuSelection) {
    case 0:
        menuMode = MenuMode::Closed;
        if (loggingEnabled)
            stopLogging();
        else
            startLogging();
        break;
    case 1:
        if (ROLE == SurveyRole::Mobile) {
            soundEnabled = !soundEnabled;
#if defined(SURVEY_ROLE_MOBILE)
            if (!soundEnabled) {
                noTone(BUZZER_PIN);
                buzzerPlaying = false;
                buzzerQueueHead = 0;
                buzzerQueueTail = 0;
                buzzerQueueCount = 0;
            }
#endif
        }
        clearMenuHoldProgress();
        drawMenuOption(menuSelection, true);
        break;
    case 2:
        screenTimeoutSelection = (screenTimeoutSelection + 1) % SCREEN_TIMEOUT_OPTION_COUNT;
        lastScreenInteractionMs = millis();
        controlStorageCacheValid = false;
        clearMenuHoldProgress();
        drawMenuOption(menuSelection, true);
        break;
    case 3:
        menuMode = MenuMode::ConfirmErase;
        showConfirmationPopup("CONFIRM WIPE", "DELETE ALL LOGS", "Hold again to erase");
        break;
    case 4:
        menuMode = MenuMode::Closed;
        showScreen("RESTARTING", "Please wait...");
        delay(500);
        NVIC_SystemReset();
        break;
    case 5:
        menuMode = MenuMode::ConfirmPowerOff;
        showConfirmationPopup("CONFIRM POWER OFF", "TURN DEVICE OFF", "Hold again to power off");
        break;
    default:
        menuMode = MenuMode::Closed;
        showLoggingStatus();
        break;
    }
}

void commitStableButtonState(bool down)
{
    buttonStableDown = down;
    if (down) {
        lastScreenInteractionMs = millis();
        buttonPressedMs = buttonChangedMs;
        buttonLongHandled = false;
        buttonHoldProgress = 0;
        if (!screenAwake) {
            screenAwake = true;
            digitalWrite(TFT_BACKLIGHT, LOW);
            suppressButtonRelease = true;
        }
        return;
    }
    if (suppressButtonRelease) {
        suppressButtonRelease = false;
    } else if (!buttonLongHandled) {
        if (static_cast<uint32_t>(buttonChangedMs - buttonPressedMs) >= BUTTON_LONG_PRESS_MS)
            handleLongButtonPress();
        else
            handleShortButtonPress();
    }
    buttonLongHandled = false;
    buttonHoldProgress = 0;
}

void applyButtonRawEdge(bool down, uint32_t edgeMs)
{
    if (down == buttonRawDown)
        return;
    // Reconstruct any stable state that occurred while radio or display work
    // kept the main loop busy. This is what preserves even a complete tap.
    if (buttonRawDown != buttonStableDown &&
        static_cast<uint32_t>(edgeMs - buttonChangedMs) >= BUTTON_DEBOUNCE_MS)
        commitStableButtonState(buttonRawDown);
    buttonRawDown = down;
    buttonChangedMs = edgeMs;
}

bool dequeueButtonEdge(bool &down, uint32_t &edgeMs)
{
    noInterrupts();
    const uint8_t tail = buttonEdgeTail;
    if (tail == buttonEdgeHead) {
        interrupts();
        return false;
    }
    edgeMs = buttonEdgeTimes[tail];
    down = buttonEdgeLevels[tail] != 0;
    buttonEdgeTail = static_cast<uint8_t>((tail + 1) % BUTTON_EDGE_QUEUE_CAPACITY);
    interrupts();
    return true;
}

void processButton()
{
    const uint32_t now = millis();
    bool edgeDown = false;
    uint32_t edgeMs = 0;
    while (dequeueButtonEdge(edgeDown, edgeMs))
        applyButtonRawEdge(edgeDown, edgeMs);

    // Reconcile the physical level in case an unusually noisy switch filled
    // the edge queue before the main loop drained it.
    const bool physicalDown = digitalRead(USER_BUTTON_PIN) == LOW;
    if (physicalDown != buttonRawDown)
        applyButtonRawEdge(physicalDown, now);

    if (buttonRawDown != buttonStableDown &&
        static_cast<uint32_t>(now - buttonChangedMs) >= BUTTON_DEBOUNCE_MS)
        commitStableButtonState(buttonRawDown);

    if (buttonStableDown && buttonRawDown && !suppressButtonRelease && !buttonLongHandled &&
        static_cast<uint32_t>(now - buttonPressedMs) >= BUTTON_LONG_PRESS_MS) {
        buttonLongHandled = true;
        buttonHoldProgress = 100;
        handleLongButtonPress();
    } else if (buttonStableDown && buttonRawDown && !suppressButtonRelease && !buttonLongHandled &&
               menuMode != MenuMode::Closed) {
        const uint32_t elapsed = static_cast<uint32_t>(now - buttonPressedMs);
        const uint8_t progress = elapsed >= BUTTON_LONG_PRESS_MS
                                     ? 100
                                     : static_cast<uint8_t>(elapsed * 100UL / BUTTON_LONG_PRESS_MS);
        if (progress != buttonHoldProgress) {
            const int16_t previousWidth = static_cast<int16_t>(204UL * buttonHoldProgress / 100UL);
            const int16_t progressWidth = static_cast<int16_t>(204UL * progress / 100UL);
            buttonHoldProgress = progress;
            if (progressWidth > previousWidth)
                display.fillRect(18 + previousWidth, 119, progressWidth - previousWidth, 4, ST77XX_YELLOW);
        }
    }
    if (menuMode != MenuMode::Closed && static_cast<uint32_t>(now - lastMenuInteractionMs) >= MENU_TIMEOUT_MS) {
        menuMode = MenuMode::Closed;
        showLoggingStatus();
    }
}

void serviceScreenTimeout()
{
    if (!screenAwake)
        return;
    const uint32_t timeoutMs = SCREEN_TIMEOUT_OPTIONS_MS[screenTimeoutSelection];
    if (!timeoutMs || static_cast<uint32_t>(millis() - lastScreenInteractionMs) < timeoutMs)
        return;
    screenAwake = false;
    digitalWrite(TFT_BACKLIGHT, HIGH);
}

int64_t daysFromCivil(int year, unsigned month, unsigned day)
{
    year -= month <= 2;
    const int era = (year >= 0 ? year : year - 399) / 400;
    const unsigned yearOfEra = static_cast<unsigned>(year - era * 400);
    const unsigned dayOfYear = (153 * (month + (month > 2 ? -3 : 9)) + 2) / 5 + day - 1;
    const unsigned dayOfEra = yearOfEra * 365 + yearOfEra / 4 - yearOfEra / 100 + dayOfYear;
    return era * 146097LL + static_cast<int>(dayOfEra) - 719468LL;
}

uint32_t gpsEpoch()
{
    if (!gps.date.isValid() || !gps.time.isValid())
        return 0;
    const int64_t days = daysFromCivil(gps.date.year(), gps.date.month(), gps.date.day());
    const int64_t seconds = days * 86400LL + gps.time.hour() * 3600LL + gps.time.minute() * 60LL + gps.time.second();
    return seconds > 0 && seconds <= UINT32_MAX ? static_cast<uint32_t>(seconds) : 0;
}

bool gpsSpeedIsFresh()
{
    return gps.speed.isValid() && gps.speed.age() < GPS_MAX_FIX_AGE_MS;
}

bool gpsDrivingMode()
{
    return gpsSpeedIsFresh() && gps.speed.kmph() >= GPS_DRIVING_SPEED_KMPH;
}

uint8_t requiredGpsGoodFixes()
{
    return gpsDrivingMode() ? GPS_DRIVING_REQUIRED_GOOD_FIXES : GPS_REQUIRED_GOOD_FIXES;
}

#if defined(SURVEY_ROLE_MOBILE)
float minimumSampleDistanceMeters()
{
    return gpsDrivingMode() ? GPS_DRIVING_MIN_SAMPLE_DISTANCE_METERS : GPS_MIN_SAMPLE_DISTANCE_METERS;
}
#endif

float allowedGpsJumpMeters(float elapsedSeconds)
{
    float allowedMetersPerSecond = GPS_JUMP_METERS_PER_SECOND;
    if (gpsSpeedIsFresh()) {
        const float speedBasedAllowance = static_cast<float>(gps.speed.mps()) * GPS_SPEED_JUMP_MULTIPLIER;
        if (speedBasedAllowance > allowedMetersPerSecond)
            allowedMetersPerSecond = speedBasedAllowance;
    }
    return GPS_JUMP_BASE_METERS + allowedMetersPerSecond * elapsedSeconds;
}

SurveyPosition currentPosition()
{
    SurveyPosition position = {};
    const bool speedReasonable = !gpsSpeedIsFresh() || gps.speed.kmph() <= GPS_MAX_TRAVEL_SPEED_KMPH;
    position.valid = gpsFixTrusted && gps.location.isValid() && gps.location.age() < GPS_MAX_FIX_AGE_MS &&
                     gps.satellites.isValid() && gps.satellites.age() < GPS_MAX_FIX_AGE_MS &&
                     gps.satellites.value() >= GPS_MIN_SATELLITES && gps.hdop.isValid() &&
                     gps.hdop.age() < GPS_MAX_FIX_AGE_MS && gps.hdop.value() <= GPS_MAX_HDOP_CENTI &&
                     speedReasonable
                         ? 1
                         : 0;
    if (!position.valid)
        return position;
    position.latitudeE7 = static_cast<int32_t>(lround(gps.location.lat() * 10000000.0));
    position.longitudeE7 = static_cast<int32_t>(lround(gps.location.lng() * 10000000.0));
    position.altitudeCm = gps.altitude.isValid() ? static_cast<int32_t>(lround(gps.altitude.meters() * 100.0)) : 0;
    const unsigned long hdop = gps.hdop.isValid() ? static_cast<unsigned long>(gps.hdop.value()) : 0;
    const unsigned long satellites = gps.satellites.isValid() ? static_cast<unsigned long>(gps.satellites.value()) : 0;
    position.hdopCenti = static_cast<uint16_t>(hdop > 65535UL ? 65535UL : hdop);
    position.satellites = static_cast<uint8_t>(satellites > 255UL ? 255UL : satellites);
    return position;
}

bool rawGpsFixMeetsQuality()
{
    if (!gps.location.isValid() || gps.location.age() >= GPS_MAX_FIX_AGE_MS)
        return false;
    if (!gps.satellites.isValid() || gps.satellites.age() >= GPS_MAX_FIX_AGE_MS ||
        gps.satellites.value() < GPS_MIN_SATELLITES)
        return false;
    if (!gps.hdop.isValid() || gps.hdop.age() >= GPS_MAX_FIX_AGE_MS || gps.hdop.value() > GPS_MAX_HDOP_CENTI)
        return false;
    return !gpsSpeedIsFresh() || gps.speed.kmph() <= GPS_MAX_TRAVEL_SPEED_KMPH;
}

void updateGpsTrust()
{
    if (!rawGpsFixMeetsQuality()) {
        gpsFixTrusted = false;
        gpsGoodFixCount = 0;
        gpsCandidateValid = false;
        return;
    }

    const double latitude = gps.location.lat();
    const double longitude = gps.location.lng();
    const uint32_t now = millis();
    if (gpsCandidateValid) {
        const float elapsedSeconds = static_cast<float>(now - gpsCandidateMs) / 1000.0F;
        const float allowedJump = allowedGpsJumpMeters(elapsedSeconds);
        const double distance = TinyGPSPlus::distanceBetween(gpsCandidateLatitude, gpsCandidateLongitude,
                                                              latitude, longitude);
        if (distance > allowedJump) {
            gpsGoodFixCount = 0;
            gpsFixTrusted = false;
        }
    }

    gpsCandidateLatitude = latitude;
    gpsCandidateLongitude = longitude;
    gpsCandidateMs = now;
    gpsCandidateValid = true;
    const uint8_t requiredFixes = requiredGpsGoodFixes();
    if (gpsGoodFixCount < requiredFixes)
        ++gpsGoodFixCount;
    gpsFixTrusted = gpsGoodFixCount >= requiredFixes;
}

#if defined(SURVEY_ROLE_MOBILE)
void showGpsWaitStatus()
{
    const unsigned long bytes = gps.charsProcessed();
    const unsigned long satellites = gps.satellites.isValid() ? gps.satellites.value() : 0;

    if (bytes < 10) {
        setDashboardStatus("GPS NO DATA", ST77XX_RED);
    } else if (!gps.location.isValid() || gps.location.age() >= GPS_MAX_FIX_AGE_MS) {
        setDashboardStatus("GPS WAIT", ST77XX_YELLOW);
    } else if (!gps.satellites.isValid() || gps.satellites.age() >= GPS_MAX_FIX_AGE_MS ||
               satellites < GPS_MIN_SATELLITES) {
        setDashboardStatus("GPS SAT LOW", ST77XX_YELLOW);
    } else if (!gps.hdop.isValid() || gps.hdop.age() >= GPS_MAX_FIX_AGE_MS ||
               gps.hdop.value() > GPS_MAX_HDOP_CENTI) {
        setDashboardStatus("GPS HDOP LOW", ST77XX_YELLOW);
    } else if (gpsSpeedIsFresh() && gps.speed.kmph() > GPS_MAX_TRAVEL_SPEED_KMPH) {
        setDashboardStatus("SPEED INVALID", ST77XX_RED);
    } else {
        setDashboardStatus("GPS STABILIZE", ST77XX_YELLOW);
    }
}
#endif

void fillPosition(SurveyRecord &record, const SurveyPosition &local, const SurveyPosition &remote)
{
    record.localLatitudeE7 = local.latitudeE7;
    record.localLongitudeE7 = local.longitudeE7;
    record.localAltitudeCm = local.altitudeCm;
    record.localHdopCenti = local.hdopCenti;
    record.localSatellites = local.satellites;
    record.remoteLatitudeE7 = remote.latitudeE7;
    record.remoteLongitudeE7 = remote.longitudeE7;
    record.remoteAltitudeCm = remote.altitudeCm;
    record.remoteHdopCenti = remote.hdopCenti;
    record.remoteSatellites = remote.satellites;
    if (local.valid)
        record.flags |= LocalGpsLock;
    if (remote.valid)
        record.flags |= RemoteGpsLock;
}

bool appendEvent(SurveyEvent event, uint32_t sequence, uint64_t peer, const SurveyPosition &local,
                 const SurveyPosition &remote, bool localRxValid, int16_t localRssi, int16_t localSnrCenti,
                 bool remoteRxValid, int16_t remoteRssi, int16_t remoteSnrCenti, uint32_t packetId,
                 uint8_t extraFlags, uint32_t eventSessionId)
{
    if (!storageReady)
        return false;
    SurveyRecord record = {};
    record.role = static_cast<uint8_t>(ROLE);
    record.event = static_cast<uint8_t>(event);
    record.sessionId = eventSessionId != 0 ? eventSessionId : sessionId;
    record.sequence = sequence;
    record.epochSeconds = gpsEpoch();
    record.uptimeMs = millis();
    record.nodeId = static_cast<uint32_t>(deviceId);
    record.peerId = static_cast<uint32_t>(peer);
    record.flags = extraFlags;
    fillPosition(record, local, remote);
    if (localRxValid)
        record.flags |= LocalRxValid;
    if (remoteRxValid)
        record.flags |= RemoteRxValid;
    record.localRssiDbm = localRssi;
    record.localSnrCentiDb = localSnrCenti;
    record.remoteRssiDbm = remoteRssi;
    record.remoteSnrCentiDb = remoteSnrCenti;
    record.packetId = packetId;
    return storage.append(record);
}

void startListening()
{
    radioInterrupt = false;
    radio.setDio1Action(onRadioInterrupt);
    radio.startReceive();
}

int16_t transmitPacket(const void *packet, size_t length)
{
    radio.clearDio1Action();
    radioInterrupt = false;
    const int16_t state = radio.transmit(static_cast<const uint8_t *>(packet), length);
    startListening();
#if defined(SURVEY_ROLE_MOBILE)
    if (state == RADIOLIB_ERR_NONE && soundEnabled)
        queueBuzzerTone(BUZZER_SEND_FREQUENCY_HZ);
#endif
    return state;
}

PendingProbe *findPending(uint32_t sequence)
{
    for (auto &probe : pending) {
        if (probe.active && probe.sequence == sequence)
            return &probe;
    }
    return nullptr;
}

#if defined(SURVEY_ROLE_MOBILE)
void sendProbe(const SurveyPosition &position)
{
    if (!loggingEnabled)
        return;
    if (!storageReady || !storage.hasSpace(1)) {
        loggingEnabled = false;
        showScreen("LOG STORAGE FULL", "Extract both radios", "No packet sent");
        return;
    }
    const uint32_t sequence = ++nextSequence;
    ProbePacket packet = {SURVEY_PROBE_MAGIC, SURVEY_FORMAT_VERSION, 1, sizeof(ProbePacket), sessionId, sequence,
                          deviceId, gpsEpoch(), millis(), position, 0};
    packet.crc32 = crc32(&packet, offsetof(ProbePacket, crc32));
    const uint32_t packetId = packet.crc32;
    const int16_t state = transmitPacket(&packet, sizeof(packet));
    if (state != RADIOLIB_ERR_NONE) {
        addDashboardRow(sequence, false, 0, 0, DashboardResponse::SendFailed);
        setDashboardStatus("RADIO ERROR", ST77XX_RED);
        return;
    }

    PendingProbe &slot = pending[sequence % PENDING_COUNT];
    slot = {true, sequence, millis(), packetId, position};
    lastSendMs = millis();
    lastGpsNoticeMs = lastSendMs;
    lastProbeLatitude = position.latitudeE7 / 10000000.0;
    lastProbeLongitude = position.longitudeE7 / 10000000.0;
    lastProbePositionValid = true;
    addDashboardRow(sequence, false, 0, 0, DashboardResponse::Waiting);
    setDashboardStatus("WAITING REPLY", ST77XX_YELLOW);
}
#endif

void handleProbe(const ProbePacket &probe, int16_t rssi, int16_t snrCenti)
{
    if (ROLE != SurveyRole::Base || probe.senderId == deviceId || !loggingEnabled)
        return;
    if (!storageReady || !storage.hasSpace(1)) {
        loggingEnabled = false;
        showScreen("LOG STORAGE FULL", "Extract both radios", "Reply suppressed");
        return;
    }
    const SurveyPosition local = currentPosition();
    ReplyPacket reply = {SURVEY_REPLY_MAGIC,
                         SURVEY_FORMAT_VERSION,
                         2,
                         sizeof(ReplyPacket),
                         probe.sessionId,
                         probe.sequence,
                         probe.senderId,
                         deviceId,
                         gpsEpoch(),
                         millis(),
                         local,
                         static_cast<int16_t>(rssi * 100),
                         snrCenti,
                         probe.crc32,
                         0};
    reply.crc32 = crc32(&reply, offsetof(ReplyPacket, crc32));
    const int16_t state = transmitPacket(&reply, sizeof(reply));
    appendEvent(SurveyEvent::ProbeRx, probe.sequence, probe.senderId, local, probe.position, true, rssi, snrCenti,
                false, 0, 0, probe.crc32, state == RADIOLIB_ERR_NONE ? ReplySent : 0, probe.sessionId);

    addDashboardRow(probe.sequence, true, rssi, snrCenti,
                    state == RADIOLIB_ERR_NONE ? DashboardResponse::ReplySent : DashboardResponse::ReplyFailed);
    setDashboardStatus("LISTENING", state == RADIOLIB_ERR_NONE ? ST77XX_GREEN : ST77XX_RED);
#if defined(SURVEY_ROLE_BASE)
    lastBasePacketMs = millis();
#endif
}

void handleReply(const ReplyPacket &reply, int16_t reverseRssi, int16_t reverseSnrCenti)
{
    if (ROLE != SurveyRole::Mobile || reply.mobileId != deviceId || reply.sessionId != sessionId || !loggingEnabled)
        return;
    PendingProbe *probe = findPending(reply.sequence);
    if (!probe)
        return;
#if defined(SURVEY_ROLE_MOBILE)
    if (soundEnabled)
        queueBuzzerTone(BUZZER_RECEIVE_FREQUENCY_HZ);
#endif
    probe->active = false;
    const int16_t forwardRssi = static_cast<int16_t>(lround(reply.forwardRssiCentiDbm / 100.0F));
    appendEvent(SurveyEvent::ReplyRx, reply.sequence, reply.baseId, probe->position, reply.basePosition, true,
                reverseRssi, reverseSnrCenti, true, forwardRssi, reply.forwardSnrCentiDb, reply.crc32);

    updateDashboardRow(reply.sequence, true, forwardRssi, reply.forwardSnrCentiDb,
                       DashboardResponse::Received);
    setDashboardStatus("GPS OK", ST77XX_GREEN);
}

void processRadio()
{
    if (!radioReady || !radioInterrupt)
        return;
    radioInterrupt = false;
    const size_t packetLength = radio.getPacketLength();
    if (!packetLength || packetLength > MAX_RADIO_PACKET_BYTES) {
        startListening();
        return;
    }
    uint8_t buffer[MAX_RADIO_PACKET_BYTES] = {};
    const int16_t state = radio.readData(buffer, packetLength);
    const int16_t rssi = static_cast<int16_t>(lround(radio.getRSSI()));
    const int16_t snrCenti = static_cast<int16_t>(lround(radio.getSNR() * 100.0F));
    startListening();
    if (state != RADIOLIB_ERR_NONE)
        return;

    if (packetLength == sizeof(ProbePacket)) {
        ProbePacket probe = {};
        memcpy(&probe, buffer, sizeof(probe));
        if (probe.magic == SURVEY_PROBE_MAGIC && probe.version == SURVEY_FORMAT_VERSION &&
            probe.size == sizeof(ProbePacket) && probe.crc32 == crc32(&probe, offsetof(ProbePacket, crc32)))
            handleProbe(probe, rssi, snrCenti);
    } else if (packetLength == sizeof(ReplyPacket)) {
        ReplyPacket reply = {};
        memcpy(&reply, buffer, sizeof(reply));
        if (reply.magic == SURVEY_REPLY_MAGIC && reply.version == SURVEY_FORMAT_VERSION &&
            reply.size == sizeof(ReplyPacket) && reply.crc32 == crc32(&reply, offsetof(ReplyPacket, crc32)))
            handleReply(reply, rssi, snrCenti);
    }
}

void expirePending()
{
    if (ROLE != SurveyRole::Mobile)
        return;
    const uint32_t now = millis();
    for (auto &probe : pending) {
        if (!probe.active || static_cast<uint32_t>(now - probe.sentAtMs) < SURVEY_REPLY_TIMEOUT_MS)
            continue;
        probe.active = false;
        appendEvent(SurveyEvent::Timeout, probe.sequence, 0, probe.position, {}, false, 0, 0, false, 0, 0,
                    probe.packetId);
        updateDashboardRow(probe.sequence, false, 0, 0, DashboardResponse::Missed);
        setDashboardStatus("GPS OK", ST77XX_GREEN);
    }
}

void dumpStorage()
{
    if (!storageReady) {
        Serial.println("MESHLAB_ERROR,STORAGE");
        return;
    }
    if (radioReady)
        radio.clearDio1Action();

    char header[160];
    snprintf(header, sizeof(header), "MESHLAB_BEGIN,%u,%s,%08lX%08lX,%lu,%u,%lu,%u,%u,%u,%d",
             storage.version(), ROLE_NAME,
             static_cast<unsigned long>(deviceId >> 32), static_cast<unsigned long>(deviceId),
             static_cast<unsigned long>(storage.slots()), static_cast<unsigned>(storage.recordSize()),
             static_cast<unsigned long>(SURVEY_FREQUENCY_HZ), static_cast<unsigned>(SURVEY_BANDWIDTH_KHZ),
             SURVEY_SPREADING_FACTOR, SURVEY_CODING_RATE, SURVEY_TX_POWER_DBM);
    Serial.println(header);
    uint32_t dumpCrc = 0xFFFFFFFFUL;
    uint8_t record[sizeof(LegacySurveyRecordV1)] = {};
    for (uint32_t slot = 0; slot < storage.slots(); ++slot) {
        memset(record, 0, sizeof(record));
        storage.readRaw(slot, record);
        Serial.write(record, storage.recordSize());
        dumpCrc = crc32Update(dumpCrc, record, storage.recordSize());
        yield();
    }
    char trailer[64];
    snprintf(trailer, sizeof(trailer), "\nMESHLAB_END,%08lX", static_cast<unsigned long>(~dumpCrc));
    Serial.println(trailer);
    Serial.flush();
    if (radioReady)
        startListening();
}

void processSerial()
{
    static char command[32] = {};
    static size_t used = 0;
    while (Serial.available()) {
        const char value = static_cast<char>(Serial.read());
        if (value == '\r')
            continue;
        if (value != '\n' && used < sizeof(command) - 1) {
            command[used++] = value;
            continue;
        }
        command[used] = '\0';
        used = 0;
        if (!strcmp(command, "MESHLAB_INFO")) {
            Serial.print("MESHLAB_STORAGE,");
            Serial.print(storageReady ? "OK" : storage.errorText());
            Serial.print(',');
            Serial.print(storage.flashBytes());
            Serial.print(',');
            Serial.print(storage.slots());
            Serial.print(',');
            Serial.println(storage.freeRecords());
            char response[160];
            snprintf(response, sizeof(response), "MESHLAB_INFO,%u,%s,%08lX%08lX,%lu,%u,%lu,%u,%u,%u,%d",
                     storage.version(), ROLE_NAME,
                     static_cast<unsigned long>(deviceId >> 32), static_cast<unsigned long>(deviceId),
                     static_cast<unsigned long>(storageReady ? storage.slots() : 0),
                     static_cast<unsigned>(storageReady ? storage.recordSize() : 0),
                     static_cast<unsigned long>(SURVEY_FREQUENCY_HZ), static_cast<unsigned>(SURVEY_BANDWIDTH_KHZ),
                     SURVEY_SPREADING_FACTOR, SURVEY_CODING_RATE, SURVEY_TX_POWER_DBM);
            Serial.println(response);
        } else if (!strcmp(command, "MESHLAB_DUMP")) {
            dumpStorage();
        } else if (!strcmp(command, "MESHLAB_CLEAR YES")) {
            if (storageReady && storage.format()) {
                loggingEnabled = false;
                clearPendingProbes();
                Serial.println("MESHLAB_CLEARED");
            } else {
                Serial.println("MESHLAB_ERROR,CLEAR");
            }
        }
    }
}

void setupDisplay()
{
    pinMode(TFT_POWER, OUTPUT);
    digitalWrite(TFT_POWER, LOW);
    pinMode(TFT_BACKLIGHT, OUTPUT);
    digitalWrite(TFT_BACKLIGHT, LOW);
    screenAwake = true;
    lastScreenInteractionMs = millis();
    delay(50);
    SPI1.setPins(255, TFT_SCK, TFT_MOSI);
    display.init(135, 240);
    display.setRotation(TFT_ROTATION);
    display.invertDisplay(true);
    showScreen("MESHLAB RF", "Starting...", ROLE_NAME);
}

void setupPeripherals()
{
    analogReference(AR_INTERNAL_3_0);
    analogReadResolution(BATTERY_ADC_RESOLUTION_BITS);
    pinMode(BATTERY_ADC_PIN, INPUT);
    pinMode(BATTERY_ADC_ENABLE_PIN, OUTPUT);
    digitalWrite(BATTERY_ADC_ENABLE_PIN, LOW);
    pinMode(USER_BUTTON_PIN, INPUT_PULLUP);
    buttonRawDown = digitalRead(USER_BUTTON_PIN) == LOW;
    buttonStableDown = buttonRawDown;
    suppressButtonRelease = buttonRawDown;
    buttonChangedMs = millis();
    buttonPressedMs = buttonChangedMs;
    attachInterrupt(digitalPinToInterrupt(USER_BUTTON_PIN), onButtonInterrupt, CHANGE);
#if defined(SURVEY_ROLE_MOBILE)
    pinMode(BUZZER_PIN, OUTPUT);
    digitalWrite(BUZZER_PIN, LOW);
#endif
    pinMode(PERIPHERAL_POWER, OUTPUT);
    digitalWrite(PERIPHERAL_POWER, HIGH);
    pinMode(GPS_STANDBY, OUTPUT);
    digitalWrite(GPS_STANDBY, HIGH);
    delay(1000);
    Serial1.setPins(GPS_CPU_RX, GPS_CPU_TX);
    Serial1.begin(9600);
}

void setupRadio()
{
    const int16_t state = radio.begin(SURVEY_FREQUENCY_MHZ, SURVEY_BANDWIDTH_KHZ, SURVEY_SPREADING_FACTOR,
                                      SURVEY_CODING_RATE, SURVEY_SYNC_WORD, SURVEY_TX_POWER_DBM,
                                      SURVEY_PREAMBLE_LENGTH, SURVEY_TCXO_VOLTAGE, false);
    if (state != RADIOLIB_ERR_NONE) {
        char error[32];
        snprintf(error, sizeof(error), "Radio init error %d", state);
        showScreen("RADIO FAILED", error);
        return;
    }
    radio.setDio2AsRfSwitch(true);
    radioReady = true;
    startListening();
}
} // namespace

void setup()
{
    Serial.begin(115200);
    deviceId = (static_cast<uint64_t>(NRF_FICR->DEVICEID[1]) << 32) | NRF_FICR->DEVICEID[0];
    randomSeed(static_cast<uint32_t>(deviceId) ^ micros());
    sessionId = static_cast<uint32_t>(random(1, INT32_MAX));

    setupPeripherals();
    setupDisplay();
    showScreen("MESHLAB RF", "Initializing log", "Internal flash region");
    storageReady = storage.begin();
    if (!storageReady) {
        showScreen("STORAGE FAILED", storage.errorText(), "Data will not persist");
    }
    setupRadio();
    if (radioReady)
        showLoggingStatus();
}

void loop()
{
#if defined(SURVEY_ROLE_MOBILE)
    serviceBuzzer();
#endif
    while (Serial1.available())
        gps.encode(Serial1.read());
    const bool locationUpdated = gps.location.isUpdated();
    if (locationUpdated) {
        updateGpsTrust();
    } else if (!gps.location.isValid() || gps.location.age() >= GPS_MAX_FIX_AGE_MS) {
        gpsFixTrusted = false;
        gpsGoodFixCount = 0;
        gpsCandidateValid = false;
    }
    processSerial();
    processButton();
    serviceTransientNotice();
    serviceScreenTimeout();
    processRadio();
    expirePending();
    if (updateBatteryReading() && menuMode == MenuMode::Closed) {
        if (controlCenterRendered && radioReady && storageReady && storage.canAppend() && storage.hasSpace(1)) {
            drawPausedGpsMetrics();
            drawControlStorageMetrics();
            drawBatteryStatus();
        } else {
            drawBatteryStatus();
        }
    }

#if defined(SURVEY_ROLE_MOBILE)
    if (loggingEnabled && radioReady && locationUpdated) {
        const SurveyPosition position = currentPosition();
        const uint32_t now = millis();
        const double latitude = position.latitudeE7 / 10000000.0;
        const double longitude = position.longitudeE7 / 10000000.0;
        const float minimumDistance = minimumSampleDistanceMeters();
        const double distance = lastProbePositionValid
                                    ? TinyGPSPlus::distanceBetween(lastProbeLatitude, lastProbeLongitude,
                                                                   latitude, longitude)
                                    : minimumDistance;
        if (position.valid && distance >= minimumDistance &&
            (!lastSendMs || static_cast<uint32_t>(now - lastSendMs) >= SURVEY_SEND_INTERVAL_MS))
            sendProbe(position);
    }
    const SurveyPosition position = currentPosition();
    if (loggingEnabled && !position.valid && static_cast<uint32_t>(millis() - lastGpsNoticeMs) >= 10000) {
        lastGpsNoticeMs = millis();
        showGpsWaitStatus();
    } else if (loggingEnabled && position.valid && lastProbePositionValid &&
               static_cast<uint32_t>(millis() - lastGpsNoticeMs) >= 10000) {
        const double distance = TinyGPSPlus::distanceBetween(
            lastProbeLatitude, lastProbeLongitude, position.latitudeE7 / 10000000.0,
            position.longitudeE7 / 10000000.0);
        const float minimumDistance = minimumSampleDistanceMeters();
        if (distance < minimumDistance) {
            lastGpsNoticeMs = millis();
            showMovementWaiting(distance, minimumDistance);
        }
    }
#else
    const uint32_t now = millis();
    if (loggingEnabled && menuMode == MenuMode::Closed &&
        static_cast<uint32_t>(now - lastBaseStatusMs) >= BASE_STATUS_REFRESH_MS &&
        (!lastBasePacketMs || static_cast<uint32_t>(now - lastBasePacketMs) >= BASE_PACKET_SCREEN_HOLD_MS)) {
        lastBaseStatusMs = now;
        const SurveyPosition position = currentPosition();
        if (position.valid)
            setDashboardStatus("GPS OK", ST77XX_GREEN);
        else if (gps.charsProcessed() < 10)
            setDashboardStatus("GPS NO DATA", ST77XX_RED);
        else
            setDashboardStatus("GPS WAIT", ST77XX_YELLOW);
    }
#endif
#if defined(SURVEY_ROLE_MOBILE)
    serviceBuzzer();
#endif
    delay(2);
}
