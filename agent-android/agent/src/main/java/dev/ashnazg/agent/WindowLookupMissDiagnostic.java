package dev.ashnazg.agent;

/** Formats bounded, non-content metadata for an opt-in missing accessibility-window trace. */
final class WindowLookupMissDiagnostic {
    private static final int MAX_WINDOW_IDS = 8;

    private WindowLookupMissDiagnostic() {}

    static String format(int targetWindowId, int totalWindows, int[] firstWindowIds) {
        int recorded = Math.min(Math.min(totalWindows, MAX_WINDOW_IDS), firstWindowIds.length);
        StringBuilder result = new StringBuilder("window-lookup-miss target=")
                .append(targetWindowId).append(" visibleIds=[");
        for (int i = 0; i < recorded; i++) {
            if (i > 0) result.append(',');
            result.append(firstWindowIds[i]);
        }
        return result.append("] omitted=").append(Math.max(0, totalWindows - recorded)).toString();
    }
}
