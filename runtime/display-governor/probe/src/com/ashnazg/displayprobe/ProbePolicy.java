package com.ashnazg.displayprobe;

import java.util.List;

/** Pure validation shared by the runtime probe and host-side tests. */
public final class ProbePolicy {
    public static final int WIDTH = 720;
    public static final int HEIGHT = 1280;
    public static final int LOW_HZ = 1;
    public static final int HIGH_HZ = 60;
    public static final int MIN_TIMEOUT_MS = 100;
    public static final int MAX_TIMEOUT_MS = 5000;

    private ProbePolicy() {}

    public static final class Mode {
        public final int id;
        public final int width;
        public final int height;
        public final float vsyncRate;

        public Mode(int id, int width, int height, float vsyncRate) {
            this.id = id;
            this.width = width;
            this.height = height;
            this.vsyncRate = vsyncRate;
        }
    }

    public static final class ModePair {
        public final Mode low;
        public final Mode high;

        private ModePair(Mode low, Mode high) {
            this.low = low;
            this.high = high;
        }
    }

    public static ModePair validateModes(List<Mode> modes) {
        if (modes == null || modes.isEmpty() || modes.size() > 64) {
            throw new IllegalArgumentException("supported mode list must contain 1..64 modes");
        }
        Mode low = null;
        Mode high = null;
        for (Mode mode : modes) {
            if (mode == null || mode.id < 0 || mode.width < 1 || mode.height < 1
                    || !Float.isFinite(mode.vsyncRate)) {
                throw new IllegalArgumentException("supported mode has invalid fields");
            }
            if (mode.width != WIDTH || mode.height != HEIGHT) {
                continue;
            }
            if (near(mode.vsyncRate, LOW_HZ)) {
                if (low != null) {
                    throw new IllegalArgumentException("720x1280 1 Hz mode is not unique");
                }
                low = mode;
            } else if (near(mode.vsyncRate, HIGH_HZ)) {
                if (high != null) {
                    throw new IllegalArgumentException("720x1280 60 Hz mode is not unique");
                }
                high = mode;
            }
        }
        if (low == null || high == null) {
            throw new IllegalArgumentException("requires unique 720x1280 1 Hz and 60 Hz modes");
        }
        if (low.id == high.id) {
            throw new IllegalArgumentException("1 Hz and 60 Hz modes must have distinct ids");
        }
        return new ModePair(low, high);
    }

    private static boolean near(float value, float expected) {
        return Math.abs(value - expected) <= 0.01f;
    }

    public static int parseTimeout(String text) {
        final int timeout;
        try {
            timeout = Integer.parseInt(text);
        } catch (RuntimeException e) {
            throw new IllegalArgumentException("timeout must be an integer from 100 to 5000 ms");
        }
        if (timeout < MIN_TIMEOUT_MS || timeout > MAX_TIMEOUT_MS) {
            throw new IllegalArgumentException("timeout must be an integer from 100 to 5000 ms");
        }
        return timeout;
    }

    /** Absolute monotonic deadline; callers must reuse it for the whole wait, not reset per poll. */
    public static final class Deadline {
        private final long deadlineNanos;

        public Deadline(int timeoutMs) {
            this(System.nanoTime(), timeoutMs);
        }

        public Deadline(long startNanos, int timeoutMs) {
            if (timeoutMs < MIN_TIMEOUT_MS || timeoutMs > MAX_TIMEOUT_MS) {
                throw new IllegalArgumentException("timeout must be an integer from 100 to 5000 ms");
            }
            this.deadlineNanos = startNanos + timeoutMs * 1_000_000L;
        }

        public long remainingMillis() {
            return remainingMillis(System.nanoTime());
        }

        public long remainingMillis(long nowNanos) {
            return Math.max(0L, (deadlineNanos - nowNanos + 999_999L) / 1_000_000L);
        }

        public boolean expired() {
            return remainingMillis() == 0L;
        }
    }
}
