#ifndef STORE_RECEIVE_RING_BUFFER_H_
#define STORE_RECEIVE_RING_BUFFER_H_
#include "protocol.h"
#include <chrono>
#include <memory>
#include <mutex>
#include <vector>

struct RcvFrameHeader {
    // Historical name: one bit per packet slot. Remote slot=(boardId-36)*2+FFT-half.
    uint64_t absolutePeriod = INVALID_PERIOD;
    uint64_t boardMask = 0;
    unsigned ifTrace = 0;
};
struct PoolStatus {
    uint64_t oldest = 0;
    uint64_t settledEnd = 0; // Exclusive upper bound of finalized periods.
    uint64_t latest = 0;
    uint64_t received = 0;
    uint64_t duplicate = 0;
    uint64_t late = 0;
    uint64_t conflicting = 0;
    uint64_t transitionCapacity = 0;
    uint64_t transitionUsed = 0;
    uint64_t transitionPeak = 0;
    uint64_t lastFreezeMilliseconds = 0;
    uint64_t freezeCount = 0;
    bool transitionWarning = false;
    bool ready = false;
};
// One instance per receive port; its mutex protects the active receive state.
// A packet slot identifies one packet position in a period. A remote physical
// board owns two adjacent slots, one for each FFT half.
//
// Normal: A and B hold two consecutive halfPeriods of rolling data.
// beginFreeze: redirect incoming packets to the bounded transition buffer.
// finishFreeze: consolidate the event into one bank; use the other for live input.
// release: after durable storage or cancellation, resume A/B rolling reception.
// Event intervals are [start, start + count); settledEnd is also exclusive.
class ReceiveRingBuffer {
  public:
    ReceiveRingBuffer(unsigned portIndex, size_t halfPeriods, size_t transitionPeriods,
                      size_t maxEventPeriods, unsigned packetSlots, unsigned packetsPerBoard,
                      unsigned packetBytes,
                      unsigned reorderPeriods, unsigned settleMilliseconds,
                      unsigned transitionWarnPercent, bool prefault);
    ~ReceiveRingBuffer();
    bool receive(uint64_t period, unsigned packetSlot, unsigned ifTrace,
                 const unsigned char *packet);
    PoolStatus status() const;
    void beginFreeze(uint64_t start, uint64_t count);
    void finishFreeze();
    void release();
    uint64_t missingPackets() const;
    void appendEventFrame(uint64_t period, std::vector<unsigned char> &out) const;
    bool isFrozen() const;
    unsigned packetSlotCount() const { return packetSlots; }
    unsigned packetsPerPhysicalBoard() const { return packetsPerBoard; }
    unsigned physicalBoardCount() const { return physicalBoards; }

  private:
    struct Allocation;
    struct BoardBuffer;
    struct Buffer;
    enum Mode {
        NORMAL,     // Both banks participate in the rolling history.
        TRANSITION, // Banks are frozen while packets enter the transition buffer.
        SINGLE      // One bank is protected; the other receives new packets.
    };
    uint64_t settledEndUnlocked() const;
    uint64_t settledEndUnlocked(std::chrono::steady_clock::time_point now) const;
    PoolStatus statusUnlocked() const;
    const Buffer *find(uint64_t p) const;
    void copySlot(const Buffer &src, Buffer &dst, uint64_t p);
    const unsigned portIndex;
    const size_t half, transitionCapacity, eventLimit;
    const unsigned packetSlots, packetsPerBoard, physicalBoards, packetBytes;
    const unsigned reorder, settleMs, transitionWarnPercent;
    const size_t frameBytes;
    // One payload allocation is partitioned into A/B/transition slabs. Each
    // BoardBuffer is a non-owning strided view into one slab.
    std::unique_ptr<Allocation> allocation;
    std::unique_ptr<Buffer> bank[2], transition;
    mutable std::mutex mutex;
    Mode mode = NORMAL;
    bool haveData = false, frozen = false, consolidated = false;
    unsigned target = 0;    // Bank reserved for the event while frozen.
    unsigned firstBank = 0; // First bank used after anchor in NORMAL mode.
    uint64_t anchor = 0;
    uint64_t oldest = 0;
    uint64_t latest = 0;
    uint64_t finalizedEnd = 0;
    uint64_t transitionStart = 0;
    uint64_t eventStart = 0;
    uint64_t eventCount = 0;
    uint64_t received = 0;
    uint64_t duplicate = 0;
    uint64_t late = 0;
    uint64_t conflicting = 0;
    uint64_t transitionPeak = 0;
    uint64_t lastFreezeMilliseconds = 0;
    uint64_t freezeCount = 0;
    bool transitionWarningLogged = false;
    std::chrono::steady_clock::time_point freezeStarted;
    std::chrono::steady_clock::time_point lastReceive;
};
#endif
