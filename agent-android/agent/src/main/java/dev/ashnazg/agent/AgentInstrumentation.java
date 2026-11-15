package dev.ashnazg.agent;

import android.app.Instrumentation;
import android.app.UiAutomation;
import android.accessibilityservice.AccessibilityServiceInfo;
import android.os.Bundle;
import android.util.Log;

public final class AgentInstrumentation extends Instrumentation {
    private static final String TAG = "AshnazgAgent";
    private volatile boolean stopped;
    private RpcServer server;
    private String token;
    private boolean traceWindowLookupMiss;

    @Override public void onCreate(Bundle arguments) {
        super.onCreate(arguments);
        token = arguments == null ? null : arguments.getString("token");
        traceWindowLookupMiss = arguments != null && Boolean.parseBoolean(arguments.getString("traceWindowLookupMiss", "false"));
        start();
    }

    boolean traceWindowLookupMissEnabled() { return traceWindowLookupMiss; }

    @Override public void onStart() {
        super.onStart();
        Bundle status = new Bundle();
        int resultCode = 0;
        try {
            if (token == null || token.isEmpty()) throw new IllegalArgumentException("instrumentation refused: missing token");
            UiAutomation automation = getUiAutomation();
            AccessibilityServiceInfo info = automation.getServiceInfo();
            if (info == null) info = new AccessibilityServiceInfo();
            info.flags |= AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS |
                    AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS | AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS;
            automation.setServiceInfo(info);
            server = new RpcServer(token, automation, this);
            Log.i(TAG, "Ashnazg Android agent 0.1.0 listening on loopback:7777");
            server.run();
        }
        catch (Exception e) {
            InstrumentationStartup.Failure failure = InstrumentationStartup.failure(e);
            Log.e(TAG, failure.message, e);
            status.putString("agentError", failure.message);
            resultCode = failure.exitCode;
        }
        finally {
            if (!stopped && server != null && resultCode == 0) {
                resultCode = 1;
                status.putString("agentError", "RPC server stopped unexpectedly");
            }
            stopped = true;
            if (server != null) server.close();
            finish(resultCode, status);
        }
    }

    void stopAgent() {
        stopped = true;
        if (server != null) server.close();
    }

    @Override public void onDestroy() { stopAgent(); super.onDestroy(); }
}
