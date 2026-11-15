package dev.ashnazg.agent;

final class LogLine {
    private LogLine() {}

    static boolean matchesTag(String line, String expectedTag) {
        return expectedTag != null && expectedTag.equals(tag(line));
    }

    static long timestampMs(String line) {
        try {
            String trimmed = line.trim();
            int separator = trimmed.indexOf(' ');
            String seconds = separator < 0 ? trimmed : trimmed.substring(0, separator);
            return (long) (Double.parseDouble(seconds) * 1000.0d);
        } catch (RuntimeException e) { return 0; }
    }

    private static String tag(String line) {
        String[] fields = line.trim().split("\\s+", 6);
        if (fields.length < 5) return null;
        String priority = fields[3];
        String rawTag;
        if (priority.length() == 1 && "VDIWEF".contains(priority)) rawTag = fields[4];
        else {
            int slash = priority.indexOf('/');
            rawTag = slash >= 0 ? priority.substring(slash + 1) : fields[4];
        }
        if (rawTag.endsWith(":")) rawTag = rawTag.substring(0, rawTag.length() - 1);
        return rawTag;
    }
}
