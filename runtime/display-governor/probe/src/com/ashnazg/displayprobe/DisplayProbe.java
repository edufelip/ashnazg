package com.ashnazg.displayprobe;

import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.List;

/** One-shot, bounded SurfaceFlinger display-mode probe. */
public final class DisplayProbe {
    private static final int MAX_OUTPUT_CHARS = 8192;
    private static final long POLL_NANOS = 50_000_000L;

    private enum Operation { INSPECT, QUALIFY }

    private static final class Options {
        final Operation operation;
        final int timeoutMs;

        Options(Operation operation, int timeoutMs) {
            this.operation = operation;
            this.timeoutMs = timeoutMs;
        }
    }

    private static final class ProbeFailure extends Exception {
        ProbeFailure(String message) { super(message); }
        ProbeFailure(String message, Throwable cause) { super(message, cause); }
    }

    private DisplayProbe() {}

    public static void main(String[] args) {
        try {
            Options options = parseArgs(args);
            if (options == null) {
                printUsage();
                return;
            }
            if (options.operation == Operation.QUALIFY) {
                int uid = currentUid();
                if (uid != 0 && uid != 1000) {
                    throw new ProbeFailure("qualification requires privileged UID 0 or 1000; got " + uid);
                }
            }
            FrameworkApi api = FrameworkApi.initialize();
            run(api, options);
        } catch (Throwable failure) {
            System.err.println("status=unavailable reason=" + safe(failure.getMessage()));
            System.exit(2);
        }
    }

    private static Options parseArgs(String[] args) throws ProbeFailure {
        Operation operation = Operation.INSPECT;
        boolean operationSpecified = false;
        boolean disposable = false;
        boolean timeoutSpecified = false;
        int timeoutMs = 2000;
        for (int i = 0; i < args.length; i++) {
            switch (args[i]) {
                case "--help":
                    if (args.length != 1) throw new ProbeFailure("--help cannot be combined with other options");
                    return null;
                case "--inspect":
                    if (operationSpecified) throw new ProbeFailure("choose exactly one operation");
                    operation = Operation.INSPECT;
                    operationSpecified = true;
                    break;
                case "--qualify-modes":
                    if (operationSpecified) throw new ProbeFailure("choose exactly one operation");
                    operation = Operation.QUALIFY;
                    operationSpecified = true;
                    break;
                case "--disposable-guest":
                    if (disposable) throw new ProbeFailure("duplicate --disposable-guest");
                    disposable = true;
                    break;
                case "--timeout-ms":
                    if (timeoutSpecified) throw new ProbeFailure("duplicate --timeout-ms");
                    timeoutSpecified = true;
                    if (++i >= args.length) throw new ProbeFailure("--timeout-ms requires a value");
                    try {
                        timeoutMs = ProbePolicy.parseTimeout(args[i]);
                    } catch (IllegalArgumentException e) {
                        throw new ProbeFailure(e.getMessage());
                    }
                    break;
                default:
                    throw new ProbeFailure("unsupported option: " + safe(args[i]));
            }
        }
        if (operation == Operation.QUALIFY && !disposable) {
            throw new ProbeFailure("--qualify-modes requires explicit --disposable-guest");
        }
        if (operation == Operation.INSPECT && disposable) {
            throw new ProbeFailure("--disposable-guest is valid only with --qualify-modes");
        }
        if (operation == Operation.INSPECT && timeoutSpecified) {
            throw new ProbeFailure("--timeout-ms is valid only with --qualify-modes");
        }
        return new Options(operation, timeoutMs);
    }

    private static void printUsage() {
        emit("Usage: DisplayProbe [--inspect | --qualify-modes --disposable-guest] [--timeout-ms 100..5000]");
        emit("Default: --inspect (read-only). No display selector or shell-command option exists.");
    }

    private static int currentUid() throws ProbeFailure {
        try {
            Class<?> process = Class.forName("android.os.Process");
            return ((Number) process.getMethod("myUid").invoke(null)).intValue();
        } catch (Throwable e) {
            throw new ProbeFailure("cannot determine Android process UID", unwrap(e));
        }
    }

    private static void run(FrameworkApi api, Options options) throws ProbeFailure {
        long[] ids = (long[]) api.invoke(api.getPhysicalDisplayIds, null);
        if (ids == null || ids.length == 0) throw new ProbeFailure("no physical displays returned");
        if (options.operation == Operation.QUALIFY && ids.length != 1) {
            throw new ProbeFailure("qualification requires exactly one physical display; got " + ids.length);
        }
        if (ids.length > 16) throw new ProbeFailure("refusing to print more than 16 physical displays");

        StringBuilder output = new StringBuilder();
        output.append("status=available operation=")
                .append(options.operation == Operation.INSPECT ? "inspect" : "qualify-modes")
                .append(" physical_display_count=").append(ids.length).append('\n');
        for (long id : ids) {
            Object info = api.invoke(api.getDynamicDisplayInfo, null, id);
            if (info == null) throw new ProbeFailure("getDynamicDisplayInfo returned null for physical display " + id);
            Object[] displayModes = (Object[]) api.field(api.dynamicModes, info);
            if (displayModes == null || displayModes.length == 0 || displayModes.length > 64) {
                throw new ProbeFailure("display mode list must contain 1..64 entries");
            }
            List<ProbePolicy.Mode> modes = api.readModes(displayModes);
            int activeId = ((Number) api.field(api.activeModeId, info)).intValue();
            output.append("display_id=").append(id).append(" active_mode_id=").append(activeId).append(" modes=");
            for (int i = 0; i < modes.size(); i++) {
                ProbePolicy.Mode mode = modes.get(i);
                if (i > 0) output.append(';');
                output.append(mode.id).append(':').append(mode.width).append('x').append(mode.height)
                        .append('@').append(mode.vsyncRate);
            }
            output.append('\n');
            if (options.operation == Operation.QUALIFY) {
                Object token = api.invoke(api.getPhysicalDisplayToken, null, id);
                if (token == null) throw new ProbeFailure("getPhysicalDisplayToken returned null");
                runQualification(api, id, token, modes, options.timeoutMs, output);
            }
        }
        emit(output.toString());
    }

    private static void runQualification(FrameworkApi api, long id, Object token,
            List<ProbePolicy.Mode> modes, int timeoutMs, StringBuilder output) throws ProbeFailure {
        ProbePolicy.ModePair pair;
        try {
            pair = ProbePolicy.validateModes(modes);
        } catch (IllegalArgumentException e) {
            throw new ProbeFailure(e.getMessage());
        }
        Object original = api.invoke(api.getDesiredSpecs, null, token);
        if (original == null) throw new ProbeFailure("getDesiredDisplayModeSpecs returned null");
        Object saved = api.copyDesiredSpecs(original);
        boolean mutationAttempted = false;
        boolean restoreSuccess = false;
        String failure = null;
        try {
            mutationAttempted = true;
            boolean lowReturn = api.requestMode(token, pair.low, ProbePolicy.LOW_HZ);
            int lowActive = api.awaitActiveMode(id, pair.low.id, timeoutMs);
            output.append("request_hz=1 set_return=").append(lowReturn)
                    .append(" observed_active_mode_id=").append(lowActive).append('\n');

            boolean highReturn = api.requestMode(token, pair.high, ProbePolicy.HIGH_HZ);
            int highActive = api.awaitActiveMode(id, pair.high.id, timeoutMs);
            output.append("request_hz=60 set_return=").append(highReturn)
                    .append(" observed_active_mode_id=").append(highActive).append('\n');
            output.append("qualification=passed observation=SurfaceControl.DynamicDisplayInfo.activeDisplayModeId\n");
        } catch (Throwable e) {
            Throwable cause = unwrap(e);
            failure = cause.getClass().getSimpleName() + ": " + safe(cause.getMessage());
        } finally {
            if (mutationAttempted) {
                try {
                    boolean restoreReturn = (Boolean) api.invoke(api.setDesiredSpecs, null, token, saved);
                    restoreSuccess = api.awaitDesiredSpecs(token, original, timeoutMs);
                    output.append("restore_success=").append(restoreSuccess)
                            .append(" set_return=").append(restoreReturn)
                            .append(" verified=getDesiredDisplayModeSpecs.equals\n");
                } catch (Throwable restoreFailure) {
                    output.append("restore_success=false reason=")
                            .append(safe(unwrap(restoreFailure).getMessage())).append('\n');
                }
            } else {
                output.append("restore_success=not-needed mutation_attempted=false\n");
            }
        }
        emit(output.toString());
        output.setLength(0);
        if (failure != null) {
            throw new ProbeFailure(failure + "; restore_success=" + restoreSuccess);
        }
        if (!restoreSuccess) throw new ProbeFailure("mode checks passed but restoring original desired specs was not verified");
    }

    private static void emit(String text) {
        String bounded = text == null ? "" : text;
        if (bounded.length() > MAX_OUTPUT_CHARS) bounded = bounded.substring(0, MAX_OUTPUT_CHARS) + "[truncated]";
        System.out.print(bounded);
        if (!bounded.endsWith("\n")) System.out.println();
    }

    private static String safe(String text) {
        if (text == null || text.isEmpty()) return "unspecified";
        String singleLine = text.replace('\n', ' ').replace('\r', ' ');
        return singleLine.length() > 240 ? singleLine.substring(0, 240) : singleLine;
    }

    private static Throwable unwrap(Throwable e) {
        while ((e instanceof InvocationTargetException) && ((InvocationTargetException) e).getCause() != null) {
            e = ((InvocationTargetException) e).getCause();
        }
        return e;
    }

    /** Cached reflection adapter for the pinned hidden framework API and its JNI registration. */
    private static final class FrameworkApi {
        final Method getPhysicalDisplayIds;
        final Method getPhysicalDisplayToken;
        final Method getDynamicDisplayInfo;
        final Method getDesiredSpecs;
        final Method setDesiredSpecs;
        final Field dynamicModes;
        final Field activeModeId;
        final Field modeId;
        final Field modeWidth;
        final Field modeHeight;
        final Field modeVsyncRate;
        final Field desiredDefaultMode;
        final Field desiredAllowGroupSwitching;
        final Field desiredPrimaryRanges;
        final Field desiredAppRequestRanges;
        final Field rangesPhysical;
        final Field rangesRender;
        final Field rangeMin;
        final Field rangeMax;
        final Class<?> rangeType;
        final Class<?> rangesType;
        final Class<?> specsType;
        final Constructor<?> rangeConstructor;
        final Constructor<?> rangesConstructor;
        final Constructor<?> specsConstructor;
        final Constructor<?> specsCopyConstructor;

        private FrameworkApi(Class<?> displayControl, Class<?> surfaceControl) throws Exception {
            getPhysicalDisplayIds = displayControl.getMethod("getPhysicalDisplayIds");
            getPhysicalDisplayToken = displayControl.getMethod("getPhysicalDisplayToken", long.class);
            getDynamicDisplayInfo = surfaceControl.getMethod("getDynamicDisplayInfo", long.class);
            Class<?> dynamicType = Class.forName("android.view.SurfaceControl$DynamicDisplayInfo");
            Class<?> modeType = Class.forName("android.view.SurfaceControl$DisplayMode");
            dynamicModes = dynamicType.getField("supportedDisplayModes");
            activeModeId = dynamicType.getField("activeDisplayModeId");
            modeId = modeType.getField("id");
            modeWidth = modeType.getField("width");
            modeHeight = modeType.getField("height");
            modeVsyncRate = modeType.getField("vsyncRate");

            specsType = Class.forName("android.view.SurfaceControl$DesiredDisplayModeSpecs");
            Class<?> rateRangeType = Class.forName("android.view.SurfaceControl$RefreshRateRange");
            rangesType = Class.forName("android.view.SurfaceControl$RefreshRateRanges");
            Class<?> idleType = Class.forName("android.view.SurfaceControl$IdleScreenRefreshRateConfig");
            rangeType = rateRangeType;
            desiredDefaultMode = specsType.getField("defaultMode");
            desiredAllowGroupSwitching = specsType.getField("allowGroupSwitching");
            desiredPrimaryRanges = specsType.getField("primaryRanges");
            desiredAppRequestRanges = specsType.getField("appRequestRanges");
            rangesPhysical = rangesType.getField("physical");
            rangesRender = rangesType.getField("render");
            rangeMin = rangeType.getField("min");
            rangeMax = rangeType.getField("max");
            getDesiredSpecs = surfaceControl.getMethod("getDesiredDisplayModeSpecs",
                    Class.forName("android.os.IBinder"));
            setDesiredSpecs = surfaceControl.getMethod("setDesiredDisplayModeSpecs",
                    Class.forName("android.os.IBinder"), specsType);
            rangeConstructor = rangeType.getConstructor(float.class, float.class);
            rangesConstructor = rangesType.getConstructor(rangeType, rangeType);
            specsConstructor = specsType.getConstructor(int.class, boolean.class,
                    rangesType, rangesType, idleType);
            specsCopyConstructor = specsType.getConstructor(specsType);
        }

        static FrameworkApi initialize() throws ProbeFailure {
            try {
                Class<?> factory = Class.forName("com.android.internal.os.ClassLoaderFactory");
                Method createClassLoader = factory.getDeclaredMethod("createClassLoader",
                        String.class, String.class, String.class, ClassLoader.class,
                        int.class, boolean.class, String.class);
                String classpath = System.getenv("SYSTEMSERVERCLASSPATH");
                if (classpath == null || classpath.isEmpty()) {
                    throw new IllegalStateException("SYSTEMSERVERCLASSPATH is unset");
                }
                ClassLoader loader = (ClassLoader) createClassLoader.invoke(null, classpath,
                        null, null, ClassLoader.getSystemClassLoader(), 0, true, null);
                Class<?> displayControl = loader.loadClass("com.android.server.display.DisplayControl");
                Method loadLibrary = Runtime.class.getDeclaredMethod("loadLibrary0", Class.class, String.class);
                loadLibrary.setAccessible(true);
                loadLibrary.invoke(Runtime.getRuntime(), displayControl, "android_servers");
                Class<?> surfaceControl = Class.forName("android.view.SurfaceControl");
                return new FrameworkApi(displayControl, surfaceControl);
            } catch (Throwable e) {
                throw new ProbeFailure("DisplayControl JNI initialization/API shape unavailable: "
                        + safe(unwrap(e).getMessage()), unwrap(e));
            }
        }

        Object invoke(Method method, Object receiver, Object... args) throws ProbeFailure {
            try {
                return method.invoke(receiver, args);
            } catch (Throwable e) {
                Throwable cause = unwrap(e);
                String message = cause.getClass().getSimpleName() + ": " + safe(cause.getMessage());
                if (cause instanceof UnsatisfiedLinkError) message = "DisplayControl JNI registration unavailable: " + safe(cause.getMessage());
                throw new ProbeFailure(message, cause);
            }
        }

        Object field(Field field, Object target) throws ProbeFailure {
            try {
                return field.get(target);
            } catch (ReflectiveOperationException e) {
                throw new ProbeFailure("cannot read framework field " + field.getName(), e);
            }
        }

        List<ProbePolicy.Mode> readModes(Object[] platformModes) throws ProbeFailure {
            List<ProbePolicy.Mode> modes = new ArrayList<>();
            for (Object platformMode : platformModes) {
                modes.add(new ProbePolicy.Mode(
                        ((Number) field(modeId, platformMode)).intValue(),
                        ((Number) field(modeWidth, platformMode)).intValue(),
                        ((Number) field(modeHeight, platformMode)).intValue(),
                        ((Number) field(modeVsyncRate, platformMode)).floatValue()));
            }
            return modes;
        }

        Object copyDesiredSpecs(Object original) throws ProbeFailure {
            try {
                return specsCopyConstructor.newInstance(original);
            } catch (ReflectiveOperationException e) {
                throw new ProbeFailure("cannot copy original desired display specs", unwrap(e));
            }
        }

        boolean requestMode(Object token, ProbePolicy.Mode mode, int hz) throws ProbeFailure {
            try {
                Object range = rangeConstructor.newInstance((float) hz, (float) hz);
                Object ranges = rangesConstructor.newInstance(range, range);
                Object request = specsConstructor.newInstance(mode.id, false, ranges, ranges, null);
                return (Boolean) invoke(setDesiredSpecs, null, token, request);
            } catch (ReflectiveOperationException e) {
                throw new ProbeFailure("cannot construct desired display mode specs", unwrap(e));
            }
        }

        int awaitActiveMode(long displayId, int expectedId, int timeoutMs) throws ProbeFailure {
            ProbePolicy.Deadline deadline = new ProbePolicy.Deadline(timeoutMs);
            while (true) {
                Object info = invoke(getDynamicDisplayInfo, null, displayId);
                if (info == null) throw new ProbeFailure("getDynamicDisplayInfo returned null while waiting");
                int active = ((Number) field(activeModeId, info)).intValue();
                if (active == expectedId) return active;
                long remaining = deadline.remainingMillis();
                if (remaining == 0) throw new ProbeFailure("timed out waiting for activeDisplayModeId=" + expectedId);
                sleep(Math.min(remaining, Math.max(1L, POLL_NANOS / 1_000_000L)));
            }
        }

        boolean awaitDesiredSpecs(Object token, Object original, int timeoutMs) throws ProbeFailure {
            ProbePolicy.Deadline deadline = new ProbePolicy.Deadline(timeoutMs);
            while (true) {
                Object current = invoke(getDesiredSpecs, null, token);
                if (current != null && original.equals(current)) return true;
                long remaining = deadline.remainingMillis();
                if (remaining == 0) return false;
                sleep(Math.min(remaining, Math.max(1L, POLL_NANOS / 1_000_000L)));
            }
        }

        private static void sleep(long millis) throws ProbeFailure {
            try {
                Thread.sleep(millis);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new ProbeFailure("interrupted while awaiting display state", e);
            }
        }
    }
}
