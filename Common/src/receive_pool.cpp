#include "receive_pool.h"
#include "log_output.h"
#include <algorithm>
#include <cstring>
#include <stdexcept>
#include <sys/mman.h>

struct ReceiveRingBuffer::Buffer {
    size_t capacity;
    size_t stride;
    size_t bytes;
    unsigned char *data;
    std::vector<RcvFrameHeader> headers;
    Buffer(size_t count, size_t frameBytes, bool prefault)
        : capacity(count), stride(frameBytes), bytes(count * frameBytes), headers(count) {
        data = static_cast<unsigned char *>(
            mmap(nullptr, bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0));
        if (data == MAP_FAILED)
            throw std::runtime_error("receive buffer mmap failed");
        madvise(data, bytes, MADV_HUGEPAGE);
        madvise(data, bytes, MADV_DONTDUMP);
        if (prefault)
            std::memset(data, 0, bytes);
    }
    ~Buffer() {
        munmap(data, bytes);
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
    void reset() {
        std::fill(headers.begin(), headers.end(), RcvFrameHeader{});
    }
};
ReceiveRingBuffer::ReceiveRingBuffer(unsigned index, size_t h, size_t t, size_t maxEvent,
                                     unsigned b, unsigned pb, unsigned r, unsigned ms,
                                     unsigned warnPercent, bool prefault)
    : portIndex(index), half(h), transitionCapacity(t), eventLimit(maxEvent), boards(b),
      packetBytes(pb), reorder(r), settleMs(ms), transitionWarnPercent(warnPercent),
      frameBytes(size_t(b) * pb) {
    if (!h || !t || !b || b > 36 || pb < 16 || !r || r >= h || maxEvent > h ||
        !warnPercent || warnPercent >= 100)
        throw std::runtime_error("invalid receive pool dimensions");
    bank[0].reset(new Buffer(h, frameBytes, prefault));
    bank[1].reset(new Buffer(h, frameBytes, prefault));
    transition.reset(new Buffer(t, frameBytes, prefault));
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
    for (const auto &b : bank)
        if (b->header(p).absolutePeriod == p)
            return b.get();
    return nullptr;
}
void ReceiveRingBuffer::copySlot(const Buffer &src, Buffer &dst, uint64_t p) {
    dst.header(p) = src.header(p);
    std::memcpy(dst.frame(p), src.frame(p), frameBytes);
}
// Write one packet to the rolling or transition area, deduplicate by period and packet position, and count late arrivals and conflicts.
bool ReceiveRingBuffer::receive(uint64_t p, unsigned b, unsigned ifTrace, const unsigned char *packet) {
    std::lock_guard<std::mutex> lock(mutex);
    if (b >= boards || p == UINT64_MAX)
        throw std::runtime_error("invalid receive index");
    // A single timestamp defines both finalization and last-receive time for this packet.
    const auto now = std::chrono::steady_clock::now();
    if (haveData && (p < oldest || p < settledEndUnlocked(now))) {
        ++late;
        return false;
    }
    if (!haveData) {
        oldest = p;
        anchor = p;
        latest = p;
        haveData = true;
    }
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
    uint64_t bit = 1ULL << b;
    if (h.boardMask & bit) {
        ++duplicate;
        return false;
    }
    std::memcpy(dst->frame(p) + b * packetBytes, packet, packetBytes);
    h.boardMask |= bit;
    ++received;
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
            for (unsigned b = 0; b < boards; ++b) {
                uint64_t bit = 1ULL << b;
                if ((srcHeader.boardMask & bit) && !(h.boardMask & bit)) {
                    std::memcpy(live.frame(p) + b * packetBytes,
                                transition->frame(p) + b * packetBytes, packetBytes);
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
        missing += boards - __builtin_popcountll(bank[target]->header(p).boardMask);
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
    put32(out, boards);
    if (h.boardMask == (1ULL << boards) - 1) {
        // Complete frames need no zero fill: append the contiguous packet block once.
        const auto *frame = src.frame(p);
        out.insert(out.end(), frame, frame + frameBytes);
        return;
    }
    size_t offset = out.size();
    out.resize(offset + frameBytes, 0);
    for (unsigned b = 0; b < boards; ++b)
        if (h.boardMask & (1ULL << b))
            std::memcpy(out.data() + offset + b * packetBytes, src.frame(p) + b * packetBytes,
                        packetBytes);
}
