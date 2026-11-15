package dev.ashnazg.agent;

final class NavigationBarrier {
    private NavigationBarrier() {}
    static boolean settled(boolean navigationOutcomeObserved, boolean quietWindow) { return navigationOutcomeObserved && quietWindow; }
    static boolean afterMutation(boolean contentEventObserved, boolean quietWindow) { return contentEventObserved && quietWindow; }
}
