#include "receive_pool.h"
#include "log_output.h"
#include <algorithm>
#include <cstring>
#include <stdexcept>
#include <sys/mman.h>

struct ReceiveRingBuffer::Allocation {
    size_t bytes;
    unsigned char *data;
    Allocation(size_t size, bool prefault) : bytes(size) {
        data = static_cast<unsigned char *>(
            mmap(nullptr, bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0));
        if (data == MAP_FAILED)
            throw std::runtime_error("receive buffer mmap failed");
        madvise(data, bytes, MADV_HUGEPAGE);
        madvise(data, bytes, MADV_DONTDUMP);
        if (prefault)
            std::memset(data, 0, bytes);
    }
    ~Allocation() { munmap(data, bytes); }
};

struct ReceiveRingBuffer::BoardBuffer {
    unsigned char *base;
    size_t capacity, periodStride, packetBytes, packetsPerPeriod;
    BoardBuffer(unsigned char *address, size_t count, size_t stride, size_t bytes,
                size_t packets)
        : base(address), capacity(count), periodStride(stride), packetBytes(bytes),
          packetsPerPeriod(packets) {}
    unsigned char *packet(uint64_t period, unsigned packetIndex) {
        return base + (period % capacity) * periodStride + packetIndex * packetBytes;
    }
    const unsigned char *packet(uint64_t period, unsigned packetIndex) const {
        return base + (period % capacity) * periodStride + packetIndex * packetBytes;
    }
};

struct ReceiveRingBuffer::Buffer {
    size_t capacity, stride, packetSize;
    unsigned packetsPerBoard;
    unsigned char *data;
    std::vector<RcvFrameHeader> headers;
    std::vector<BoardBuffer> boardViews;
    Buffer(unsigned char *address, size_t count, size_t frameBytes,
           unsigned packetSlots, unsigned packetsPerPhysicalBoard, size_t packetBytes)
        : capacity(count), stride(frameBytes), packetSize(packetBytes),
          packetsPerBoard(packetsPerPhysicalBoard),
          data(address), headers(count) {
        const unsigned physicalBoards = packetSlots / packetsPerBoard;
        boardViews.reserve(physicalBoards);
        for (unsigned board = 0; board < physicalBoards; ++board)
            boardViews.emplace_back(data + board * packetsPerBoard * packetBytes,
                                    count, frameBytes, packetBytes, packetsPerBoard);
    }
    RcvFrameHeader &header(uint64_t p) {
        return headers[p % capacity];
    }
    const RcvFrameHeader &header(uint64_t p) const {
        return headers[p % capacity];
    }
    unsigned char *frame(uint64_t p) {
        return data + (p % capacity) * stride;
    }
    const unsigned char *frame(uint64_t p) const {
        return data + (p % capacity) * stride;
    }
    unsigned char *packet(uint64_t p, unsigned packetSlot) {
        // The logical board views describe ownership, but ingress uses the
        // contiguous frame directly to avoid division and a vector lookup for
        // every packet in the receive hot path.
        return frame(p) + size_t(packetSlot) * packetSize;
    }
    const unsigned char *packet(uint64_t p, unsigned packetSlot) const {
        return frame(p) + size_t(packetSlot) * packetSize;
    }
    void reset() {
        std::fill(headers.begin(), headers.end(), RcvFrameHeader{});
    }
};
ReceiveRingBuffer::ReceiveRingBuffer(unsigned index, size_t h, size_t t, size_t maxEvent,
                                     unsigned b, unsigned ppb, unsigned pb, unsigned r, unsigned ms,
                                     unsigned warnPercent, bool prefault)
    : portIndex(index), half(h), transitionCapacity(t), eventLimit(maxEvent), packetSlots(b),
      packetsPerBoard(ppb), physicalBoards(ppb ? b / ppb : 0), packetBytes(pb), reorder(r),
      settleMs(ms), transitionWarnPercent(warnPercent),
      frameBytes(size_t(b) * pb) {
    if (!h || !t || !b || b > 36 || !ppb || ppb > 2 || b % ppb || b / ppb > 36 ||
        pb < 16 || !r || r >= h || maxEvent > h ||
        !warnPercent || warnPercent >= 100)
        throw std::runtime_error("invalid receive pool dimensions");
    const size_t bankBytes = h * frameBytes;
    const size_t transitionBytes = t * frameBytes;
    allocation.reset(new Allocation(2 * bankBytes + transitionBytes, prefault));
    bank[0].reset(new Buffer(allocation->data, h, frameBytes, packetSlots,
                             packetsPerBoard, packetBytes));
    bank[1].reset(new Buffer(allocation->data + bankBytes, h, frameBytes, packetSlots,
                             packetsPerBoard, packetBytes));
    transition.reset(new Buffer(allocation->data + 2 * bankBytes, t, frameBytes,
                                packetSlots, packetsPerBoard, packetBytes));
}
ReceiveRingBuffer::~ReceiveRingBuffer() = default;
uint64_t ReceiveRingBuffer::settledEndUnlocked() const {
    return settledEndUnlocked(std::chrono::steady_clock::now());
}
// Calculate the exclusive upper bound of finalized periods; the caller must hold the mutex.
uint64_t ReceiveRingBuffer::settledEndUnlocked(std::chrono::steady_clock::time_point now) const {
    if (!haveData)
        return 0;
    uint64_t end = latest >= reorder ? latest - reorder + 1 : 0;
    if (now - lastReceive >= std::chrono::milliseconds(settleMs))
        end = latest + 1;
    return std::max(end, finalizedEnd);
}
PoolStatus ReceiveRingBuffer::statusUnlocked() const {
    PoolStatus s;
    s.oldest = oldest;
    s.latest = latest;
    s.settledEnd = settledEndUnlocked();
    s.ready = haveData && !frozen && mode == NORMAL && latest + 1 - oldest >= 2 * half;
    s.received = received;
    s.duplicate = duplicate;
    s.late = late;
    s.conflicting = conflicting;
    s.lost = lostPackets;
    s.lossPeriods = lossPeriods;
    s.lastLossPeriod = lastLossPeriod;
    s.transitionCapacity = transitionCapacity;
    if (mode == TRANSITION && latest >= transitionStart)
        s.transitionUsed = std::min<uint64_t>(transitionCapacity, latest - transitionStart + 1);
    s.transitionPeak = transitionPeak;
    s.lastFreezeMilliseconds = lastFreezeMilliseconds;
    s.freezeCount = freezeCount;
    s.transitionWarning = mode == TRANSITION &&
        s.transitionUsed * 100 >= transitionCapacity * transitionWarnPercent;
    return s;
}
PoolStatus ReceiveRingBuffer::status() const {
    std::lock_guard<std::mutex> lock(mutex);
    return statusUnlocked();
}
const ReceiveRingBuffer::Buffer *ReceiveRingBuffer::find(uint64_t p) const {
    if (mode == TRANSITION && p >= transitionStart &&
        transition->header(p).absolutePeriod == p)
        return transition.get();
    for (const auto &b : bank)
        if (b->header(p).absolutePeriod == p)
            return b.get();
    return nullptr;
}
void ReceiveRingBuffer::addPacketLoss(uint64_t firstPeriod, uint64_t lastPeriod,
                                      uint64_t missing, uint64_t affected) {
    if (!affected)
        return;
    if (!pendingMissingPeriods)
        pendingFirstMissingPeriod = firstPeriod;
    pendingLastMissingPeriod = lastPeriod;
    pendingMissingPackets += missing;
    pendingMissingPeriods += affected;
    lostPackets += missing;
    lossPeriods += affected;
    lastLossPeriod = lastPeriod;
}
void ReceiveRingBuffer::logPacketLoss(std::chrono::steady_clock::time_point now) {
    if (!pendingMissingPeriods)
        return;
    constexpr auto interval = std::chrono::seconds(1);
    if (lastLossWarning.time_since_epoch().count() && now - lastLossWarning < interval)
        return;
    LOG("Receive port " + std::to_string(portIndex) +
            " packet loss: missing_packets=" + std::to_string(pendingMissingPackets) +
            " affected_periods=" + std::to_string(pendingMissingPeriods) +
            " period_range=" + std::to_string(pendingFirstMissingPeriod) + ".." +
            std::to_string(pendingLastMissingPeriod),
        WARN);
    pendingMissingPackets = 0;
    pendingMissingPeriods = 0;
    lastLossWarning = now;
}
// Check only periods beyond the reorder window. A large sequence jump is
// counted as a range instead of scanning every absent period.
void ReceiveRingBuffer::detectPacketLoss(std::chrono::steady_clock::time_point now,
                                         uint64_t previousLatest) {
    const uint64_t settledEnd = settledEndUnlocked(now);
    const uint64_t scanEnd = std::min(settledEnd, previousLatest + 1);
    const uint64_t expectedMask = (1ULL << packetSlots) - 1;
    while (nextLossCheck < scanEnd) {
        const auto *buffer = find(nextLossCheck);
        const uint64_t receivedMask = buffer ? buffer->header(nextLossCheck).boardMask : 0;
        const uint64_t missing =
            packetSlots - static_cast<uint64_t>(__builtin_popcountll(receivedMask & expectedMask));
        if (missing)
            addPacketLoss(nextLossCheck, nextLossCheck, missing, 1);
        ++nextLossCheck;
    }
    if (nextLossCheck < settledEnd) {
        const uint64_t affected = settledEnd - nextLossCheck;
        addPacketLoss(nextLossCheck, settledEnd - 1, affected * packetSlots, affected);
        nextLossCheck = settledEnd;
    }
    logPacketLoss(now);
}
void ReceiveRingBuffer::copySlot(const Buffer &src, Buffer &dst, uint64_t p) {
    dst.header(p) = src.header(p);
    for (unsigned packetSlot = 0; packetSlot < packetSlots; ++packetSlot)
        std::memcpy(dst.packet(p, packetSlot), src.packet(p, packetSlot), packetBytes);
}
// Write one packet to the rolling or transition area, deduplicate by period and packet position, and count late arrivals and conflicts.
bool ReceiveRingBuffer::receive(uint64_t p, unsigned packetSlot, unsigned ifTrace,
                                const unsigned char *packet) {
    std::lock_guard<std::mutex> lock(mutex);
    if (packetSlot >= packetSlots || p == UINT64_MAX)
        throw std::runtime_error("invalid receive index");
    // Sequence-based finalization handles the normal high-rate path without a
    // clock read for every board packet. Timeout-based finalization is sampled
    // when the integration period advances and by status/freeze operations.
    const uint64_t sequenceSettledEnd = haveData
        ? std::max(finalizedEnd, latest >= reorder ? latest - reorder + 1 : 0)
        : 0;
    if (haveData && (p < oldest || p < sequenceSettledEnd)) {
        ++late;
        return false;
    }
    const bool periodAdvanced = !haveData || p > latest;
    // A sparse fallback sample keeps idle-settlement timing representative
    // during unusual out-of-order traffic without restoring per-packet clocks.
    const bool sampleClock = periodAdvanced || (received & 63U) == 0;
    std::chrono::steady_clock::time_point now;
    if (sampleClock)
        now = std::chrono::steady_clock::now();
    if (!haveData) {
        oldest = p;
        anchor = p;
        latest = p;
        nextLossCheck = p;
        haveData = true;
    }
    const uint64_t previousLatest = latest;
    if (mode == TRANSITION) {
        if (p >= transitionStart + transitionCapacity)
            throw std::runtime_error("transition buffer overflow; event cannot be guaranteed");
        if (p >= transitionStart) {
            const uint64_t used = p - transitionStart + 1;
            transitionPeak = std::max(transitionPeak, used);
            if (!transitionWarningLogged &&
                used * 100 >= transitionCapacity * transitionWarnPercent) {
                transitionWarningLogged = true;
                LOG("Receive port " + std::to_string(portIndex) +
                        " transition buffer at " + std::to_string(used) + "/" +
                        std::to_string(transitionCapacity) + " periods",
                    WARN);
            }
        }
    }
    if (p > latest)
        latest = p;
    const uint64_t retention = mode == NORMAL ? 2 * half : half;
    if (latest + 1 > retention)
        oldest = std::max(oldest, latest + 1 - retention);
    if (sampleClock)
        lastReceive = now;

    // During consolidation both original banks are read-only. After it, only
    // the bank not holding the event may receive new packets.
    Buffer *dst;
    if (mode == TRANSITION) {
        dst = transition.get();
    } else if (mode == SINGLE || p < anchor) {
        dst = bank[1 - target].get();
    } else {
        dst = bank[(firstBank + (p - anchor) / half) % 2].get();
    }
    auto &h = dst->header(p);
    if (h.absolutePeriod != p)
        h = {p, 0, ifTrace};
    if (h.ifTrace != ifTrace) {
        ++conflicting;
        return false;
    }
    uint64_t bit = 1ULL << packetSlot;
    if (h.boardMask & bit) {
        ++duplicate;
        return false;
    }
    std::memcpy(dst->packet(p, packetSlot), packet, packetBytes);
    h.boardMask |= bit;
    ++received;
    // Only the first packet that advances the period can finalize older
    // periods. Avoid repeating the same scan for every board packet.
    if (latest > previousLatest)
        detectPacketLoss(now, previousLatest);
    return true;
}
// Validate the event range and redirect subsequent packets to the transition area so data pending storage is not overwritten during collation.
void ReceiveRingBuffer::beginFreeze(uint64_t start, uint64_t count) {
    std::lock_guard<std::mutex> lock(mutex);
    auto s = statusUnlocked();
    if (!s.ready)
        throw std::runtime_error("BUSY_OR_WARMING");
    if (!count || count > eventLimit || start > UINT64_MAX - count)
        throw std::runtime_error("INVALID_RANGE");
    if (start < oldest)
        throw std::runtime_error("EXPIRED");
    if (start + count > s.settledEnd)
        throw std::runtime_error("NOT_READY");
    const uint64_t nextTransitionStart = s.settledEnd;
    const uint64_t unsettledPeriods =
        latest >= nextTransitionStart ? latest - nextTransitionStart + 1 : 0;
    if (unsettledPeriods > transitionCapacity)
        throw std::runtime_error("transition buffer too small for unsettled tail");
    size_t inBank[2] = {};
    for (uint64_t p = start; p < start + count; ++p) {
        for (unsigned b = 0; b < 2; ++b)
            if (bank[b]->header(p).absolutePeriod == p)
                ++inBank[b];
    }
    target = inBank[1] > inBank[0] ? 1 : 0;
    // Preserve the mutable tail, including packets already received for these periods.
    eventStart = start;
    eventCount = count;
    transition->reset();
    transitionStart = nextTransitionStart;
    finalizedEnd = nextTransitionStart;
    transitionPeak = unsettledPeriods;
    transitionWarningLogged = false;
    freezeStarted = std::chrono::steady_clock::now();
    for (uint64_t p = transitionStart; p <= latest; ++p) {
        if (const auto *src = find(p))
            copySlot(*src, *transition, p);
    }
    if (transitionPeak * 100 >= transitionCapacity * transitionWarnPercent) {
        transitionWarningLogged = true;
        LOG("Receive port " + std::to_string(portIndex) + " transition buffer at " +
                std::to_string(transitionPeak) + "/" + std::to_string(transitionCapacity) +
                " periods",
            WARN);
    }
    frozen = true;
    consolidated = false;
    mode = TRANSITION;
}
// Collate the event into one primary buffer, resume reception in the other, and merge data received during the transition.
void ReceiveRingBuffer::finishFreeze() {
    // beginFreeze redirects ingress to transition, so A and B are immutable here.
    // Only the event's smaller bank portion is copied; p%half is unique for count<=half.
    if (!frozen || mode != TRANSITION)
        throw std::runtime_error("invalid freeze transition");
    Buffer &dst = *bank[target];
    for (uint64_t p = eventStart; p < eventStart + eventCount; ++p) {
        if (dst.header(p).absolutePeriod == p)
            continue;
        const auto &src = *bank[1 - target];
        if (src.header(p).absolutePeriod == p)
            copySlot(src, dst, p);
        else
            dst.header(p) = {p, 0,
                             0}; // Entire missing period; payload generated as zero on export.
    }
    uint64_t stageEnd;
    {
        std::lock_guard<std::mutex> lock(mutex);
        bank[1 - target]->reset();
        stageEnd = latest + 1;
        oldest = transitionStart;
        if (latest + 1 > half)
            oldest = std::max(oldest, latest + 1 - half);
        mode = SINGLE;
    }
    // Transition is now immutable. Merge by period/board; never overwrite newer live packets.
    for (uint64_t p = transitionStart; p < stageEnd; ++p) {
        const auto &srcHeader = transition->header(p);
        if (srcHeader.absolutePeriod != p)
            continue;
        std::lock_guard<std::mutex> lock(mutex);
        if (p < oldest)
            continue;
        Buffer &live = *bank[1 - target];
        auto &h = live.header(p);
        if (h.absolutePeriod != p) {
            if (h.absolutePeriod != INVALID_PERIOD && h.absolutePeriod > p)
                continue;
            copySlot(*transition, live, p);
        } else {
            if (h.ifTrace != srcHeader.ifTrace) {
                ++conflicting;
                continue;
            }
            for (unsigned b = 0; b < packetSlots; ++b) {
                uint64_t bit = 1ULL << b;
                if ((srcHeader.boardMask & bit) && !(h.boardMask & bit)) {
                    std::memcpy(live.packet(p, b), transition->packet(p, b), packetBytes);
                    h.boardMask |= bit;
                }
            }
        }
    }
    std::lock_guard<std::mutex> lock(mutex);
    consolidated = true;
    lastFreezeMilliseconds = static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now() - freezeStarted)
            .count());
    ++freezeCount;
}
// Release the collated event area, retain live data, and resume double-buffer rotation.
void ReceiveRingBuffer::release() {
    std::lock_guard<std::mutex> lock(mutex);
    if (!frozen)
        return;
    if (!consolidated)
        throw std::runtime_error("cannot release an unfinished freeze");
    // Retain the live 50s; fill the released bank next. No bulk data reset/copy.
    anchor = latest + 1;
    firstBank = target;
    finalizedEnd = std::max(finalizedEnd, settledEndUnlocked());
    mode = NORMAL;
    frozen = false;
    consolidated = false;
}
bool ReceiveRingBuffer::isFrozen() const {
    std::lock_guard<std::mutex> lock(mutex);
    return frozen;
}
uint64_t ReceiveRingBuffer::missingPackets() const {
    // Called only after finishFreeze; release cannot run while export is active.
    uint64_t missing = 0;
    for (uint64_t p = eventStart; p < eventStart + eventCount; ++p)
        missing += packetSlots - __builtin_popcountll(bank[target]->header(p).boardMask);
    return missing;
}
// Export one period and its missing-packet bitmap; zero-fill missing positions and preserve received payload packets unchanged.
void ReceiveRingBuffer::appendEventFrame(uint64_t p, std::vector<unsigned char> &out) const {
    if (!consolidated || p < eventStart || p >= eventStart + eventCount)
        throw std::runtime_error("event frame out of range");
    const auto &src = *bank[target];
    const auto &h = src.header(p);
    put64(out, p);
    put64(out, h.boardMask);
    put32(out, packetBytes);
    put32(out, packetSlots);
    if (h.boardMask == (1ULL << packetSlots) - 1) {
        // Complete frames need no zero fill: append the contiguous packet block once.
        const auto *frame = src.frame(p);
        out.insert(out.end(), frame, frame + frameBytes);
        return;
    }
    size_t offset = out.size();
    out.resize(offset + frameBytes, 0);
    for (unsigned b = 0; b < packetSlots; ++b)
        if (h.boardMask & (1ULL << b))
            std::memcpy(out.data() + offset + b * packetBytes, src.packet(p, b), packetBytes);
}
