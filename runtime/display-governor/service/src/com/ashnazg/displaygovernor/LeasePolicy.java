package com.ashnazg.displaygovernor;

import java.util.HashMap;
import java.util.Iterator;
import java.util.Map;
import java.util.Objects;
import java.util.function.LongSupplier;

/** Deterministic policy for short-lived display-mode leases. */
public final class LeasePolicy {
    private static final long SECOND = 1_000_000_000L;
    private static final long LEASE_TTL_NANOS = 30L * SECOND;
    private static final long QUIET_GRACE_NANOS = 2L * SECOND;
    private static final int MAX_ACTIVE_LEASES = 16;
    private static final String CANONICAL_UUID =
            "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";

    private static final class Lease {
        private long expiresAt;
        private long sequence;

        private Lease(long expiresAt) {
            this.expiresAt = expiresAt;
        }
    }

    private final LongSupplier clock;
    private final Map<String, Lease> leases = new HashMap<>();
    private long idleDeadline;

    public LeasePolicy(LongSupplier clock) {
        this.clock = Objects.requireNonNull(clock, "clock");
        this.idleDeadline = clock.getAsLong() + QUIET_GRACE_NANOS;
    }

    public synchronized boolean acquire(String leaseId) {
        long now = clock.getAsLong();
        validateId(leaseId);
        purgeExpired(now);

        if (leases.containsKey(leaseId)) {
            return false;
        }
        if (leases.size() == MAX_ACTIVE_LEASES) {
            throw new IllegalStateException("maximum active display leases reached");
        }

        leases.put(leaseId, new Lease(now + LEASE_TTL_NANOS));
        return true;
    }

    public synchronized boolean renew(String leaseId, long sequence) {
        long now = clock.getAsLong();
        validateId(leaseId);
        if (sequence <= 0) {
            throw new IllegalArgumentException("sequence must be positive");
        }
        purgeExpired(now);

        Lease lease = leases.get(leaseId);
        if (lease == null || lease.sequence == Long.MAX_VALUE || sequence != lease.sequence + 1) {
            return false;
        }
        lease.sequence = sequence;
        lease.expiresAt = now + LEASE_TTL_NANOS;
        return true;
    }

    public synchronized boolean release(String leaseId) {
        long now = clock.getAsLong();
        validateId(leaseId);
        purgeExpired(now);

        if (leases.remove(leaseId) == null) {
            return false;
        }
        if (leases.isEmpty()) {
            idleDeadline = now + QUIET_GRACE_NANOS;
        }
        return true;
    }

    public synchronized int desiredHz() {
        long now = clock.getAsLong();
        purgeExpired(now);
        return leases.isEmpty() && deadlineReached(now, idleDeadline) ? 1 : 60;
    }

    public synchronized int activeCount() {
        long now = clock.getAsLong();
        purgeExpired(now);
        return leases.size();
    }

    public synchronized long nanosUntilNextChange() {
        long now = clock.getAsLong();
        purgeExpired(now);
        if (leases.isEmpty()) {
            return deadlineReached(now, idleDeadline) ? 0 : idleDeadline - now;
        }

        long earliest = Long.MAX_VALUE;
        for (Lease lease : leases.values()) {
            long remaining = lease.expiresAt - now;
            if (remaining < earliest) {
                earliest = remaining;
            }
        }
        return earliest;
    }

    public synchronized long remainingNanos(String leaseId) {
        long now = clock.getAsLong();
        validateId(leaseId);
        purgeExpired(now);
        Lease lease = leases.get(leaseId);
        return lease == null ? 0 : lease.expiresAt - now;
    }

    private void purgeExpired(long now) {
        if (leases.isEmpty()) {
            return;
        }

        Iterator<Map.Entry<String, Lease>> iterator = leases.entrySet().iterator();
        long latestExpiredAt = 0;
        boolean expiredAny = false;
        while (iterator.hasNext()) {
            Lease lease = iterator.next().getValue();
            if (deadlineReached(now, lease.expiresAt)) {
                if (!expiredAny || lease.expiresAt - latestExpiredAt > 0) {
                    latestExpiredAt = lease.expiresAt;
                }
                expiredAny = true;
                iterator.remove();
            }
        }

        if (expiredAny && leases.isEmpty()) {
            idleDeadline = latestExpiredAt + QUIET_GRACE_NANOS;
        }
    }

    private static boolean deadlineReached(long now, long deadline) {
        return now - deadline >= 0;
    }

    private static void validateId(String leaseId) {
        if (leaseId == null || !leaseId.matches(CANONICAL_UUID)) {
            throw new IllegalArgumentException("leaseId must be a canonical lowercase UUID");
        }
    }
}
