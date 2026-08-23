#include <Adafruit_GFX.h>
#include <Adafruit_SPIFlash.h>
#include <Adafruit_ST7789.h>
#include <Arduino.h>
#include <RadioLib.h>
#include <TinyGPS++.h>
#include <SPI.h>
#include <cstddef>
#include <cmath>
#include <cstring>

#include "survey_config.h"
#include "survey_protocol.h"

#if !defined(SURVEY_ROLE_MOBILE) && !defined(SURVEY_ROLE_BASE)
#error "Select either the mobile or base PlatformIO environment"
#endif

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

TinyGPSPlus gps;
Adafruit_ST7789 display(&SPI1, TFT_CS, TFT_DC, TFT_RESET);
SX1262 radio = new Module(LORA_CS, LORA_DIO1, LORA_RESET, LORA_BUSY);
Adafruit_FlashTransport_QSPI flashTransport;
Adafruit_SPIFlash flash(&flashTransport);

volatile bool radioInterrupt = false;
bool radioReady = false;
bool storageReady = false;
uint64_t deviceId = 0;
uint32_t sessionId = 0;
#if defined(SURVEY_ROLE_MOBILE)
uint32_t nextSequence = 0;
uint32_t lastSendMs = 0;
uint32_t lastGpsNoticeMs = 0;
#endif

struct PendingProbe {
    bool active;
    uint32_t sequence;
    uint32_t sentAtMs;
    uint32_t packetId;
    SurveyPosition position;
};

PendingProbe pending[PENDING_COUNT] = {};

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

class SurveyStorage
{
  public:
    bool begin()
    {
        if (!flash.begin() || flash.size() < SURVEY_STORAGE_BYTES)
            return false;
        partitionStart = flash.size() - SURVEY_STORAGE_BYTES;

        StorageHeader header = {};
        flash.readBuffer(partitionStart, reinterpret_cast<uint8_t *>(&header), sizeof(header));
        const bool headerValid = header.magic == SURVEY_STORAGE_MAGIC && header.version == SURVEY_FORMAT_VERSION &&
                                 header.recordSize == sizeof(SurveyRecord) &&
                                 header.partitionBytes == SURVEY_STORAGE_BYTES &&
                                 header.crc32 == crc32(&header, offsetof(StorageHeader, crc32));
        if (!headerValid && !format())
            return false;

        scan();
        return true;
    }

    bool format()
    {
        for (uint32_t address = partitionStart; address < partitionStart + SURVEY_STORAGE_BYTES;
             address += FLASH_SECTOR_BYTES) {
            if (!flash.eraseSector(address / FLASH_SECTOR_BYTES))
                return false;
        }

        StorageHeader header = {SURVEY_STORAGE_MAGIC, SURVEY_FORMAT_VERSION, sizeof(SurveyRecord),
                                SURVEY_STORAGE_BYTES, SURVEY_FREQUENCY_HZ, 0};
        header.crc32 = crc32(&header, offsetof(StorageHeader, crc32));
        if (flash.writeBuffer(partitionStart, reinterpret_cast<uint8_t *>(&header), sizeof(header)) != sizeof(header))
            return false;
        slotCount = 0;
        return true;
    }

    bool append(SurveyRecord &record)
    {
        if (slotCount >= capacity())
            return false;
        record.magic = SURVEY_RECORD_MAGIC;
        record.version = SURVEY_FORMAT_VERSION;
        record.crc32 = crc32(&record, offsetof(SurveyRecord, crc32));
        const uint32_t address = recordAddress(slotCount);
        const size_t written =
            flash.writeBuffer(address, reinterpret_cast<uint8_t *>(&record), sizeof(SurveyRecord));
        if (written != sizeof(SurveyRecord))
            return false;
        ++slotCount;
        return true;
    }

    uint32_t slots() const { return slotCount; }

    uint32_t capacity() const
    {
        return (SURVEY_STORAGE_BYTES - SURVEY_STORAGE_HEADER_BYTES) / sizeof(SurveyRecord);
    }

    bool hasSpace(uint32_t records) const { return records <= capacity() - slotCount; }

    bool read(uint32_t slot, SurveyRecord &record)
    {
        if (slot >= slotCount)
            return false;
        return flash.readBuffer(recordAddress(slot), reinterpret_cast<uint8_t *>(&record), sizeof(record)) ==
               sizeof(record);
    }

  private:
    uint32_t partitionStart = 0;
    uint32_t slotCount = 0;

    uint32_t recordAddress(uint32_t slot) const
    {
        return partitionStart + SURVEY_STORAGE_HEADER_BYTES + slot * sizeof(SurveyRecord);
    }

    void scan()
    {
        SurveyRecord record = {};
        slotCount = 0;
        while (slotCount < capacity()) {
            flash.readBuffer(recordAddress(slotCount), reinterpret_cast<uint8_t *>(&record), sizeof(record));
            if (allErased(reinterpret_cast<const uint8_t *>(&record), sizeof(record)))
                break;
            ++slotCount;
        }
    }
};

SurveyStorage storage;

void onRadioInterrupt()
{
    radioInterrupt = true;
}

void showScreen(const char *title, const char *line1 = nullptr, const char *line2 = nullptr, const char *line3 = nullptr)
{
    display.fillScreen(ST77XX_BLACK);
    display.setTextWrap(false);
    display.setTextColor(ROLE == SurveyRole::Mobile ? ST77XX_CYAN : ST77XX_YELLOW);
    display.setTextSize(2);
    display.setCursor(4, 4);
    display.print(title);
    display.drawFastHLine(0, 24, 240, ST77XX_WHITE);
    display.setTextColor(ST77XX_WHITE);
    display.setTextSize(2);
    if (line1) {
        display.setCursor(4, 34);
        display.print(line1);
    }
    if (line2) {
        display.setCursor(4, 62);
        display.print(line2);
    }
    display.setTextSize(1);
    if (line3) {
        display.setCursor(4, 102);
        display.print(line3);
    }
    display.setCursor(4, 122);
    display.print(ROLE_NAME);
    display.print("  ID ");
    display.print(static_cast<uint32_t>(deviceId), HEX);
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

SurveyPosition currentPosition()
{
    SurveyPosition position = {};
    position.valid = gps.location.isValid() && gps.location.age() < 3000 ? 1 : 0;
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
                 const SurveyPosition &remote, bool localRxValid = false, int16_t localRssi = 0,
                 int16_t localSnrCenti = 0, bool remoteRxValid = false, int16_t remoteRssi = 0,
                 int16_t remoteSnrCenti = 0, uint32_t packetId = 0)
{
    if (!storageReady)
        return false;
    SurveyRecord record = {};
    record.role = static_cast<uint8_t>(ROLE);
    record.event = static_cast<uint8_t>(event);
    record.sessionId = sessionId;
    record.sequence = sequence;
    record.epochSeconds = gpsEpoch();
    record.uptimeMs = millis();
    record.nodeId = deviceId;
    record.peerId = peer;
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
    record.frequencyHz = SURVEY_FREQUENCY_HZ;
    record.bandwidthKhz = static_cast<uint16_t>(SURVEY_BANDWIDTH_KHZ);
    record.spreadingFactor = SURVEY_SPREADING_FACTOR;
    record.codingRate = SURVEY_CODING_RATE;
    record.txPowerDbm = SURVEY_TX_POWER_DBM;
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
    if (!storageReady || !storage.hasSpace(2)) {
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
        char error[32];
        snprintf(error, sizeof(error), "Radio error %d", state);
        showScreen("SEND FAILED", error, "No sample logged");
        return;
    }

    PendingProbe &slot = pending[sequence % PENDING_COUNT];
    slot = {true, sequence, millis(), packetId, position};
    lastSendMs = millis();
    appendEvent(SurveyEvent::Send, sequence, 0, position, {}, false, 0, 0, false, 0, 0, packetId);

    char first[32];
    char second[32];
    snprintf(first, sizeof(first), "Probe #%lu sent", static_cast<unsigned long>(sequence));
    snprintf(second, sizeof(second), "%u sat HDOP %.2f", position.satellites, position.hdopCenti / 100.0F);
    showScreen("PACKET SENT", first, second, "Waiting for base reply");
}
#endif

void handleProbe(const ProbePacket &probe, int16_t rssi, int16_t snrCenti)
{
    if (ROLE != SurveyRole::Base || probe.senderId == deviceId)
        return;
    if (!storageReady || !storage.hasSpace(2)) {
        showScreen("LOG STORAGE FULL", "Extract both radios", "Reply suppressed");
        return;
    }
    const SurveyPosition local = currentPosition();
    appendEvent(SurveyEvent::ProbeRx, probe.sequence, probe.senderId, local, probe.position, true, rssi, snrCenti,
                false, 0, 0, probe.crc32);

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
    if (state == RADIOLIB_ERR_NONE)
        appendEvent(SurveyEvent::ReplyTx, probe.sequence, probe.senderId, local, probe.position, true, rssi, snrCenti,
                    false, 0, 0, reply.crc32);

    char first[32];
    char second[32];
    char third[40];
    snprintf(first, sizeof(first), "Probe #%lu RX", static_cast<unsigned long>(probe.sequence));
    snprintf(second, sizeof(second), "%d dBm / %.2f dB", rssi, snrCenti / 100.0F);
    snprintf(third, sizeof(third), "%s | base GPS %s", state == RADIOLIB_ERR_NONE ? "Reply sent" : "REPLY FAILED",
             local.valid ? "OK" : "NO LOCK");
    showScreen("PACKET RECEIVED", first, second, third);
}

void handleReply(const ReplyPacket &reply, int16_t reverseRssi, int16_t reverseSnrCenti)
{
    if (ROLE != SurveyRole::Mobile || reply.mobileId != deviceId || reply.sessionId != sessionId)
        return;
    PendingProbe *probe = findPending(reply.sequence);
    if (!probe)
        return;
    probe->active = false;
    const int16_t forwardRssi = static_cast<int16_t>(lround(reply.forwardRssiCentiDbm / 100.0F));
    appendEvent(SurveyEvent::ReplyRx, reply.sequence, reply.baseId, probe->position, reply.basePosition, true,
                reverseRssi, reverseSnrCenti, true, forwardRssi, reply.forwardSnrCentiDb, reply.crc32);

    char first[32];
    char second[32];
    char third[40];
    snprintf(first, sizeof(first), "Reply #%lu RX", static_cast<unsigned long>(reply.sequence));
    snprintf(second, sizeof(second), "Back %d / %.2f", reverseRssi, reverseSnrCenti / 100.0F);
    snprintf(third, sizeof(third), "Out %.2f dBm / %.2f dB | GPS %s", reply.forwardRssiCentiDbm / 100.0F,
             reply.forwardSnrCentiDb / 100.0F, reply.basePosition.valid ? "OK" : "NO");
    showScreen("REPLY RECEIVED", first, second, third);
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
        char first[32];
        snprintf(first, sizeof(first), "Probe #%lu", static_cast<unsigned long>(probe.sequence));
        showScreen("REPLY TIMEOUT", first, "Packet loss logged", "Base log tells which direction");
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

    char header[128];
    snprintf(header, sizeof(header), "MESHLAB_BEGIN,1,%s,%08lX%08lX,%lu,%u", ROLE_NAME,
             static_cast<unsigned long>(deviceId >> 32), static_cast<unsigned long>(deviceId),
             static_cast<unsigned long>(storage.slots()), static_cast<unsigned>(sizeof(SurveyRecord)));
    Serial.println(header);
    uint32_t dumpCrc = 0xFFFFFFFFUL;
    SurveyRecord record = {};
    for (uint32_t slot = 0; slot < storage.slots(); ++slot) {
        if (!storage.read(slot, record))
            memset(&record, 0, sizeof(record));
        Serial.write(reinterpret_cast<const uint8_t *>(&record), sizeof(record));
        dumpCrc = crc32Update(dumpCrc, reinterpret_cast<const uint8_t *>(&record), sizeof(record));
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
            char response[128];
            snprintf(response, sizeof(response), "MESHLAB_INFO,1,%s,%08lX%08lX,%lu,%u", ROLE_NAME,
                     static_cast<unsigned long>(deviceId >> 32), static_cast<unsigned long>(deviceId),
                     static_cast<unsigned long>(storageReady ? storage.slots() : 0),
                     static_cast<unsigned>(sizeof(SurveyRecord)));
            Serial.println(response);
        } else if (!strcmp(command, "MESHLAB_DUMP")) {
            dumpStorage();
        } else if (!strcmp(command, "MESHLAB_CLEAR YES")) {
            if (storageReady && storage.format()) {
                appendEvent(SurveyEvent::Boot, 0, 0, currentPosition(), {});
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
    delay(50);
    SPI1.setPins(255, TFT_SCK, TFT_MOSI);
    display.init(135, 240);
    display.setRotation(1);
    display.invertDisplay(true);
    showScreen("MESHLAB RF", "Starting...", ROLE_NAME);
}

void setupPeripherals()
{
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
    showScreen("MESHLAB RF", "Initializing log", "Reserved QSPI region");
    storageReady = storage.begin();
    if (!storageReady) {
        showScreen("STORAGE FAILED", "Data will not persist");
    } else {
        appendEvent(SurveyEvent::Boot, 0, 0, currentPosition(), {});
    }
    setupRadio();
    if (radioReady) {
        showScreen(ROLE == SurveyRole::Mobile ? "MOBILE READY" : "BASE READY",
                   ROLE == SurveyRole::Mobile ? "Waiting GPS lock" : "Listening for probes",
                   "906.875 MHz LF");
    }
}

void loop()
{
    while (Serial1.available())
        gps.encode(Serial1.read());
    processSerial();
    processRadio();
    expirePending();

#if defined(SURVEY_ROLE_MOBILE)
    if (radioReady && gps.location.isUpdated()) {
        const SurveyPosition position = currentPosition();
        const uint32_t now = millis();
        if (position.valid && (!lastSendMs || static_cast<uint32_t>(now - lastSendMs) >= SURVEY_SEND_INTERVAL_MS))
            sendProbe(position);
    }
    if ((!gps.location.isValid() || gps.location.age() >= 3000) &&
        static_cast<uint32_t>(millis() - lastGpsNoticeMs) >= 10000) {
        lastGpsNoticeMs = millis();
        showScreen("WAITING FOR GPS", "No location lock", "Move into open sky");
    }
#endif
    delay(2);
}
