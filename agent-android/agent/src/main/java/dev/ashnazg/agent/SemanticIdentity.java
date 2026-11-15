package dev.ashnazg.agent;

final class SemanticIdentity {
    private final String testId;
    private final String text;
    private final String role;
    private final boolean password;

    SemanticIdentity(String testId, String text, String role, boolean password) {
        this.testId = testId == null ? "" : testId;
        this.text = password || text == null ? "" : text;
        this.role = role == null ? "" : role;
        this.password = password;
    }

    boolean matches(SemanticIdentity other) {
        return other != null && testId.equals(other.testId) && text.equals(other.text) && role.equals(other.role) && password == other.password;
    }
}
