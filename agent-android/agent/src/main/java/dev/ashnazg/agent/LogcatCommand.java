package dev.ashnazg.agent;

import java.util.Locale;

final class LogcatCommand {
    private LogcatCommand() {}
    static String sinceEpochMillis(long epochMillis) {
        return "logcat -v epoch -T " + String.format(Locale.US, "%.3f", epochMillis / 1000.0d);
    }
}
