package dev.ashnazg.agent;

final class NavigationBarrier {
    private NavigationBarrier() {}
    static boolean settled(boolean windowEventObserved, boolean quietWindow) { return windowEventObserved && quietWindow; }
    static boolean afterMutation(boolean contentEventObserved, boolean quietWindow) { return contentEventObserved && quietWindow; }
}
