package dev.ashnazg.agent;

final class NodeText {
    private NodeText() {}
    static String snapshotText(CharSequence value, boolean password, boolean showingHint) {
        return value == null || password || showingHint ? "" : value.toString();
    }
    static String appendBase(CharSequence value, boolean password, boolean showingHint) {
        if (password && !showingHint && value != null && value.length() > 0)
            throw new IllegalArgumentException("clear the password field before typing; existing secure text cannot be read safely");
        return value == null || showingHint ? "" : value.toString();
    }
    static boolean isNoop(CharSequence current, boolean append, String requested, boolean password, boolean showingHint) {
        if (password && !showingHint && current != null && current.length() > 0) return false;
        String before = snapshotText(current, password, showingHint);
        String next = append ? appendBase(current, password, showingHint) + (requested == null ? "" : requested) :
                (requested == null ? "" : requested);
        return before.equals(next);
    }

    static boolean matchesExpected(CharSequence actual, String expected, boolean password, boolean showingHint) {
        String wanted = expected == null ? "" : expected;
        if (wanted.isEmpty()) return showingHint || actual == null || actual.length() == 0;
        if (showingHint || actual == null) return false;
        if (!password) return actual.toString().contentEquals(wanted);
        // Password values never leave this comparison. Android may expose a same-length mask.
        String observed = actual.toString();
        if (observed.contentEquals(wanted)) return true;
        if (observed.length() != wanted.length()) return false;
        for (int i = 0; i < observed.length(); i++) {
            char c = observed.charAt(i);
            if (c != '\u2022' && c != '\u25cf' && c != '*') return false;
        }
        return true;
    }
}
