import shutil
import subprocess
from pathlib import Path

import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
JAVA_SOURCE = (
    REPO_ROOT
    / "runtime/display-governor/service/src/com/ashnazg/displaygovernor/LeasePolicy.java"
)


HARNESS = r"""
import com.ashnazg.displaygovernor.LeasePolicy;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;

public final class LeasePolicyHarness {
    private static final long SECOND = 1_000_000_000L;

    private static void check(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }

    private static String id(int value) {
        return String.format("00000000-0000-0000-0000-%012x", value);
    }

    private static void expectIllegalArgument(Runnable action, String message) {
        try {
            action.run();
            throw new AssertionError(message);
        } catch (IllegalArgumentException expected) {
            // Expected.
        }
    }

    private static void startupAndTransitions() {
        AtomicLong clock = new AtomicLong(0);
        LeasePolicy policy = new LeasePolicy(clock::get);
        check(policy.desiredHz() == 60, "starts at 60 Hz");
        check(policy.nanosUntilNextChange() == 2 * SECOND, "startup grace remaining");
        clock.set(2 * SECOND);
        check(policy.desiredHz() == 1, "settles at 1 Hz after startup grace");
        check(policy.nanosUntilNextChange() == 0, "settled idle has no deadline");

        clock.set(10 * SECOND);
        check(policy.acquire(id(1)), "first lease acquired");
        check(policy.desiredHz() == 60, "active lease requests 60 Hz");
        check(policy.activeCount() == 1, "active count after acquire");
        check(policy.remainingNanos(id(1)) == 30 * SECOND, "initial TTL");
        check(policy.nanosUntilNextChange() == 30 * SECOND, "next change is expiry");
    }

    private static void specifiedTransitionExample() {
        AtomicLong clock = new AtomicLong(0);
        LeasePolicy policy = new LeasePolicy(clock::get);
        check(policy.desiredHz() == 60, "example starts at 60 Hz");
        clock.set(2 * SECOND);
        check(policy.desiredHz() == 1, "example settles at 2 seconds");
        check(policy.acquire(id(11)), "example lease acquired at 2 seconds");
        clock.set(31 * SECOND);
        check(policy.desiredHz() == 60, "example lease active at 31 seconds");
        clock.set(34 * SECOND);
        check(policy.desiredHz() == 1, "example settles at 34 seconds");
    }

    private static void duplicateDoesNotExtend() {
        AtomicLong clock = new AtomicLong(5 * SECOND);
        LeasePolicy policy = new LeasePolicy(clock::get);
        check(policy.acquire(id(2)), "lease acquired");
        clock.set(20 * SECOND);
        check(!policy.acquire(id(2)), "duplicate active acquire refused");
        check(policy.remainingNanos(id(2)) == 15 * SECOND, "duplicate did not extend TTL");
        check(!policy.renew(id(2), 2), "duplicate acquire did not reset sequence");
        check(policy.renew(id(2), 1), "first sequence accepted");
        check(!policy.renew(id(2), 1), "repeated sequence refused");
        check(!policy.renew(id(2), 3), "skipped sequence refused");
        check(policy.renew(id(2), 2), "next consecutive sequence accepted");
        check(policy.remainingNanos(id(2)) == 30 * SECOND, "renewal extends from now");
        expectIllegalArgument(() -> policy.renew(id(2), 0), "zero sequence rejected");
        expectIllegalArgument(() -> policy.renew(id(2), -1), "negative sequence rejected");
    }

    private static void expiryAndRenewCannotRevive() {
        AtomicLong clock = new AtomicLong(0);
        LeasePolicy policy = new LeasePolicy(clock::get);
        check(policy.acquire(id(3)), "lease acquired");
        clock.set(29 * SECOND);
        check(policy.renew(id(3), 1), "renewal before expiration succeeds");
        clock.set(59 * SECOND);
        check(policy.remainingNanos(id(3)) == 0, "expired remaining is zero");
        check(!policy.renew(id(3), 2), "expired lease cannot be renewed");
        check(!policy.release(id(3)), "expired lease release is false");
        check(policy.activeCount() == 0, "expired lease purged");
        check(policy.desiredHz() == 60, "expiry begins quiet grace at original deadline");
        check(policy.nanosUntilNextChange() == 2 * SECOND, "expiry quiet deadline is fixed");
        clock.set(61 * SECOND);
        check(policy.desiredHz() == 1, "settles after expiry quiet grace");
    }

    private static void releaseAndQuietDeadline() {
        AtomicLong clock = new AtomicLong(10 * SECOND);
        LeasePolicy policy = new LeasePolicy(clock::get);
        check(policy.acquire(id(4)), "lease acquired");
        clock.set(15 * SECOND);
        check(policy.release(id(4)), "active lease released");
        check(!policy.release(id(4)), "repeated release is false");
        check(policy.desiredHz() == 60, "release starts quiet grace");
        check(policy.nanosUntilNextChange() == 2 * SECOND, "quiet grace remaining");
        clock.set(16 * SECOND);
        check(policy.nanosUntilNextChange() == SECOND, "quiet deadline does not slide");
        clock.set(17 * SECOND);
        check(policy.desiredHz() == 1, "idle after original quiet deadline");
        check(policy.nanosUntilNextChange() == 0, "idle settled");
    }

    private static void expiryGraceDoesNotSlideWhenObservedLate() {
        AtomicLong clock = new AtomicLong(0);
        LeasePolicy policy = new LeasePolicy(clock::get);
        check(policy.acquire(id(5)), "lease acquired");
        clock.set(100 * SECOND);
        check(policy.desiredHz() == 1, "first purge at 100 seconds honors original expiry grace");
        check(policy.activeCount() == 0, "late purge removes expired lease");
        check(policy.nanosUntilNextChange() == 0, "original expiry plus grace already passed");
    }

    private static void overlapAndGraceCancellation() {
        AtomicLong clock = new AtomicLong(0);
        LeasePolicy policy = new LeasePolicy(clock::get);
        check(policy.acquire(id(6)), "first lease acquired");
        clock.set(10 * SECOND);
        check(policy.acquire(id(7)), "overlapping lease acquired");
        clock.set(30 * SECOND);
        check(policy.activeCount() == 1, "first lease expiry leaves second active");
        check(policy.desiredHz() == 60, "overlap keeps 60 Hz");
        check(policy.nanosUntilNextChange() == 10 * SECOND, "next active expiry");
        clock.set(40 * SECOND);
        check(policy.desiredHz() == 60, "last expiry enters grace");
        check(policy.nanosUntilNextChange() == 2 * SECOND, "last expiry grace");
        clock.set(41 * SECOND);
        check(policy.acquire(id(8)), "new lease acquired during grace");
        check(policy.nanosUntilNextChange() == 30 * SECOND, "new lease cancels idle deadline");
        clock.set(71 * SECOND);
        check(policy.desiredHz() == 60, "new expiry has its own grace");
        clock.set(73 * SECOND);
        check(policy.desiredHz() == 1, "new expiry grace eventually settles");
    }

    private static void capacityAndValidation() {
        AtomicLong clock = new AtomicLong(0);
        LeasePolicy policy = new LeasePolicy(clock::get);
        for (int i = 1; i <= 16; i++) check(policy.acquire(id(100 + i)), "capacity lease acquired");
        check(!policy.acquire(id(101)), "existing retry at capacity remains false");
        long firstBefore = policy.remainingNanos(id(101));
        expectIllegalState(() -> policy.acquire(id(117)), "17th distinct lease rejected");
        check(policy.activeCount() == 16, "capacity rejection leaves map unchanged");
        check(policy.remainingNanos(id(101)) == firstBefore, "capacity rejection leaves deadlines unchanged");
        check(policy.remainingNanos(id(999)) == 0, "absent lease has zero remaining");
        expectIllegalArgument(() -> policy.acquire(null), "null id rejected");
        expectIllegalArgument(() -> policy.acquire(""), "empty id rejected");
        expectIllegalArgument(() -> policy.acquire("not-a-uuid"), "malformed id rejected");
        expectIllegalArgument(() -> policy.acquire("00000000-0000-0000-0000-00000000000A"), "uppercase id rejected");
        expectIllegalArgument(() -> policy.release("00000000-0000-0000-0000-00000000000A"), "release validates id");
        expectIllegalArgument(() -> policy.remainingNanos("bad"), "remaining validates id");
        expectIllegalArgument(() -> policy.renew("bad", 1), "renew validates id");
    }

    private static void largeSequenceRejected() {
        AtomicLong clock = new AtomicLong(0);
        LeasePolicy policy = new LeasePolicy(clock::get);
        check(policy.acquire(id(9)), "lease acquired");
        check(policy.renew(id(9), 1), "first sequence accepted");
        check(!policy.renew(id(9), Long.MAX_VALUE), "skipped large sequence refused");
    }

    private static void nanoTimeWraparound() {
        AtomicLong clock = new AtomicLong(Long.MAX_VALUE - SECOND);
        LeasePolicy policy = new LeasePolicy(clock::get);
        check(policy.desiredHz() == 60, "startup grace before wrap");
        clock.set(Long.MIN_VALUE + SECOND);
        check(policy.desiredHz() == 1, "startup deadline comparison survives wrap");
        check(policy.acquire(id(10)), "lease acquired near wrap");
        clock.set(Long.MIN_VALUE + 30 * SECOND);
        check(policy.remainingNanos(id(10)) == SECOND, "lease TTL subtraction survives wrap");
        clock.set(Long.MIN_VALUE + 31 * SECOND);
        check(policy.remainingNanos(id(10)) == 0, "lease expires across wrap");
        check(policy.desiredHz() == 60, "quiet grace starts at wrapped expiry");

        long origin = Long.MAX_VALUE - SECOND;
        AtomicLong crossingClock = new AtomicLong(origin);
        LeasePolicy crossing = new LeasePolicy(crossingClock::get);
        check(crossing.acquire(id(12)), "lease acquired before signed wrap");
        crossingClock.set(origin + 29 * SECOND);
        check(crossing.remainingNanos(id(12)) == SECOND, "lease deadline crosses signed wrap");
        crossingClock.set(origin + 30 * SECOND);
        check(crossing.activeCount() == 0, "crossing lease expires at original deadline");
        check(crossing.nanosUntilNextChange() == 2 * SECOND, "crossing expiry retains quiet grace");
        crossingClock.set(origin + 32 * SECOND);
        check(crossing.desiredHz() == 1, "crossing grace ends at original deadline");
    }

    private static void concurrentAcquireRelease() throws Exception {
        AtomicLong clock = new AtomicLong(0);
        LeasePolicy policy = new LeasePolicy(clock::get);
        int workers = 8;
        CountDownLatch ready = new CountDownLatch(workers);
        CountDownLatch start = new CountDownLatch(1);
        List<Thread> threads = new ArrayList<>();
        AtomicReference<Throwable> failure = new AtomicReference<>();
        for (int i = 0; i < workers; i++) {
            final int worker = i;
            Thread thread = new Thread(() -> {
                try {
                    ready.countDown();
                    start.await();
                    for (int j = 0; j < 500; j++) {
                        String leaseId = id(1000 + worker);
                        policy.acquire(leaseId);
                        if (policy.activeCount() < 0 || policy.activeCount() > 16) {
                            throw new AssertionError("active count outside bound");
                        }
                        policy.desiredHz();
                        policy.nanosUntilNextChange();
                        policy.remainingNanos(leaseId);
                        policy.release(leaseId);
                    }
                } catch (Throwable t) {
                    failure.compareAndSet(null, t);
                }
            });
            threads.add(thread);
            thread.start();
        }
        ready.await();
        start.countDown();
        for (Thread thread : threads) thread.join();
        if (failure.get() != null) throw new AssertionError("concurrent policy operation failed", failure.get());
        check(policy.activeCount() == 0, "concurrent releases leave no active leases");
        check(policy.desiredHz() == 60, "last concurrent release starts grace");
        check(policy.nanosUntilNextChange() == 2 * SECOND, "concurrent quiet grace deadline");
    }

    private static void expectIllegalState(Runnable action, String message) {
        try {
            action.run();
            throw new AssertionError(message);
        } catch (IllegalStateException expected) {
            // Expected.
        }
    }

    public static void main(String[] args) throws Exception {
        startupAndTransitions();
        specifiedTransitionExample();
        duplicateDoesNotExtend();
        expiryAndRenewCannotRevive();
        releaseAndQuietDeadline();
        expiryGraceDoesNotSlideWhenObservedLate();
        overlapAndGraceCancellation();
        capacityAndValidation();
        largeSequenceRejected();
        nanoTimeWraparound();
        concurrentAcquireRelease();
        System.out.println("LeasePolicy contract passed");
    }
}
"""


class DisplayLeaseTests(unittest.TestCase):
    def test_display_lease_policy_contract(self):
        javac = shutil.which("javac")
        java = shutil.which("java")
        if not javac or not java:
            self.skipTest("Java 17 JDK is not installed")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            harness = root / "LeasePolicyHarness.java"
            harness.write_text(HARNESS)
            classes = root / "classes"
            classes.mkdir()
            subprocess.run(
                [javac, "--release", "17", "-d", str(classes), str(JAVA_SOURCE), str(harness)],
                cwd=REPO_ROOT, check=True, capture_output=True, text=True, timeout=30,
            )
            result = subprocess.run(
                [java, "-ea", "-cp", str(classes), "LeasePolicyHarness"],
                cwd=REPO_ROOT, check=True, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.stdout.strip(), "LeasePolicy contract passed")


if __name__ == "__main__":
    unittest.main()
