#pragma once

#include <Arduino.h>

constexpr uint32_t SURVEY_RECORD_MAGIC = 0x3152464DUL; // "MFR1"
constexpr uint32_t SURVEY_STORAGE_MAGIC = 0x314C534DUL; // "MSL1"
constexpr uint32_t SURVEY_PROBE_MAGIC = 0x3150524DUL; // "MRP1"
constexpr uint32_t SURVEY_REPLY_MAGIC = 0x3152524DUL; // "MRR1"
constexpr uint8_t SURVEY_FORMAT_VERSION = 1;

enum class SurveyRole : uint8_t {
    Mobile = 1,
    Base = 2,
};

enum class SurveyEvent : uint8_t {
    Boot = 0,
    Send = 1,
    ProbeRx = 2,
    ReplyTx = 3,
    ReplyRx = 4,
    Timeout = 5,
    StorageFull = 6,
};

enum SurveyFlags : uint8_t {
    LocalGpsLock = 1 << 0,
    RemoteGpsLock = 1 << 1,
    LocalRxValid = 1 << 2,
    RemoteRxValid = 1 << 3,
};

struct __attribute__((packed)) SurveyPosition {
    int32_t latitudeE7;
    int32_t longitudeE7;
    int32_t altitudeCm;
    uint16_t hdopCenti;
    uint8_t satellites;
    uint8_t valid;
};

struct __attribute__((packed)) ProbePacket {
    uint32_t magic;
    uint8_t version;
    uint8_t type;
    uint16_t size;
    uint32_t sessionId;
    uint32_t sequence;
    uint64_t senderId;
    uint32_t epochSeconds;
    uint32_t uptimeMs;
    SurveyPosition position;
    uint32_t crc32;
};

struct __attribute__((packed)) ReplyPacket {
    uint32_t magic;
    uint8_t version;
    uint8_t type;
    uint16_t size;
    uint32_t sessionId;
    uint32_t sequence;
    uint64_t mobileId;
    uint64_t baseId;
    uint32_t epochSeconds;
    uint32_t uptimeMs;
    SurveyPosition basePosition;
    int16_t forwardRssiCentiDbm;
    int16_t forwardSnrCentiDb;
    uint32_t probePacketId;
    uint32_t crc32;
};

struct __attribute__((packed)) SurveyRecord {
    uint32_t magic;
    uint8_t version;
    uint8_t role;
    uint8_t event;
    uint8_t flags;
    uint32_t sessionId;
    uint32_t sequence;
    uint32_t epochSeconds;
    uint32_t uptimeMs;
    uint64_t nodeId;
    uint64_t peerId;
    int32_t localLatitudeE7;
    int32_t localLongitudeE7;
    int32_t localAltitudeCm;
    uint16_t localHdopCenti;
    uint8_t localSatellites;
    uint8_t reserved0;
    int32_t remoteLatitudeE7;
    int32_t remoteLongitudeE7;
    int32_t remoteAltitudeCm;
    uint16_t remoteHdopCenti;
    uint8_t remoteSatellites;
    uint8_t reserved1;
    int16_t localRssiDbm;
    int16_t localSnrCentiDb;
    int16_t remoteRssiDbm;
    int16_t remoteSnrCentiDb;
    uint32_t packetId;
    uint32_t frequencyHz;
    uint16_t bandwidthKhz;
    uint8_t spreadingFactor;
    uint8_t codingRate;
    int8_t txPowerDbm;
    uint8_t reserved[31];
    uint32_t crc32;
};

struct __attribute__((packed)) StorageHeader {
    uint32_t magic;
    uint16_t version;
    uint16_t recordSize;
    uint32_t partitionBytes;
    uint32_t frequencyHz;
    uint32_t crc32;
};

static_assert(sizeof(SurveyPosition) == 16, "SurveyPosition wire size changed");
static_assert(sizeof(ProbePacket) == 52, "ProbePacket wire size changed");
static_assert(sizeof(ReplyPacket) == 68, "ReplyPacket wire size changed");
static_assert(sizeof(SurveyRecord) == 128, "SurveyRecord storage size changed");
static_assert(sizeof(StorageHeader) == 20, "StorageHeader size changed");
