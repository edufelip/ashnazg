use ashnazg::mcp::{ActParams, Action, Direction, Target};
use ashnazg::{validate_loopback_addr, validate_ui_act};
use serde_json::{Value, json};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    net::TcpListener,
    sync::oneshot,
};

#[test]
fn guest_address_must_be_loopback() {
    assert!(validate_loopback_addr("127.0.0.1:7777").is_ok());
    assert!(validate_loopback_addr("[::1]:7777").is_ok());
    assert!(validate_loopback_addr("0.0.0.0:7777").is_err());
    assert!(validate_loopback_addr("192.168.1.2:7777").is_err());
}

#[test]
fn ui_act_schema_rejects_invalid_action_and_selector_shapes() {
    assert!(validate_ui_act(&serde_json::json!({"action":"tap","target":{"text":"Save"}})).is_ok());
    assert!(
        validate_ui_act(
            &serde_json::json!({"action":"tap","target":{"text":"Save","id":3,"snapshotId":1}})
        )
        .is_err()
    );
    assert!(
        validate_ui_act(
            &serde_json::json!({"action":"tap","target":{"text":"Save"},"text":"ignored"})
        )
        .is_err()
    );
    assert!(validate_ui_act(&serde_json::json!({"action":"hover"})).is_err());
    assert!(validate_ui_act(&serde_json::json!({"action":"type","text":"x"})).is_err());
    assert!(validate_ui_act(&serde_json::json!({"action":"back","direction":"up"})).is_err());
    assert!(validate_ui_act(&serde_json::json!({"action":"deep_link","target":{"testId":"x"},"text":"ashnazg://fixture"})).is_err());
    assert!(
        validate_ui_act(&serde_json::json!({"action":"deep_link","text":"ashnazg://fixture"}))
            .is_ok()
    );
}

#[test]
fn every_typed_action_serializes_to_a_valid_guest_payload_without_null_optionals() {
    let selector = || Target {
        test_id: Some("dev.example:id/button".into()),
        text: None,
        id: None,
        snapshot_id: None,
    };
    let id_selector = || Target {
        test_id: None,
        text: None,
        id: Some(7),
        snapshot_id: Some(2),
    };
    let values = [
        serde_json::to_value(ActParams {
            action: Action::Tap,
            target: Some(selector()),
            text: None,
            direction: None,
        })
        .unwrap(),
        serde_json::to_value(ActParams {
            action: Action::LongPress,
            target: Some(selector()),
            text: None,
            direction: None,
        })
        .unwrap(),
        serde_json::to_value(ActParams {
            action: Action::Type,
            target: Some(id_selector()),
            text: Some("hello".into()),
            direction: None,
        })
        .unwrap(),
        serde_json::to_value(ActParams {
            action: Action::Clear,
            target: Some(selector()),
            text: None,
            direction: None,
        })
        .unwrap(),
        serde_json::to_value(ActParams {
            action: Action::ScrollTo,
            target: Some(selector()),
            text: None,
            direction: Some(Direction::Down),
        })
        .unwrap(),
        serde_json::to_value(ActParams {
            action: Action::ScrollTo,
            target: Some(selector()),
            text: None,
            direction: None,
        })
        .unwrap(),
        serde_json::to_value(ActParams {
            action: Action::Back,
            target: None,
            text: None,
            direction: None,
        })
        .unwrap(),
        serde_json::to_value(ActParams {
            action: Action::DeepLink,
            target: None,
            text: Some("ashnazg://fixture".into()),
            direction: None,
        })
        .unwrap(),
    ];
    assert_eq!(values.len(), 8);
    for value in values {
        assert!(
            validate_ui_act(&value).is_ok(),
            "invalid serialized action payload: {value}"
        );
        assert!(
            !value.to_string().contains("null"),
            "serialized action includes null fields: {value}"
        );
    }
}

#[tokio::test]
async fn guest_client_rejects_mismatched_response_id() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut stream = BufReader::new(stream);
        let mut line = String::new();
        stream.read_line(&mut line).await.unwrap();
        let request: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(request["token"], "test-token");
        stream
            .get_mut()
            .write_all(b"{\"jsonrpc\":\"2.0\",\"id\":999,\"result\":{}}\n")
            .await
            .unwrap();
    });
    let client = ashnazg::GuestClient::new(addr, "test-token".into());
    assert!(matches!(
        client.call("ui_snapshot", json!({})).await,
        Err(ashnazg::GuestError::Correlation)
    ));
    server.await.unwrap();
}

#[tokio::test]
async fn guest_client_reuses_one_tcp_connection_for_serial_calls() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut stream = BufReader::new(stream);
        for expected_id in 1..=2 {
            let mut line = String::new();
            stream.read_line(&mut line).await.unwrap();
            let request: Value = serde_json::from_str(&line).unwrap();
            assert_eq!(request["id"], expected_id);
            assert_eq!(request["token"], "test-token");
            let response =
                json!({"jsonrpc":"2.0","id":expected_id,"result":{"request":expected_id}});
            stream
                .get_mut()
                .write_all(format!("{response}\n").as_bytes())
                .await
                .unwrap();
        }
    });
    let client = ashnazg::GuestClient::new(addr, "test-token".into());
    assert_eq!(
        client.call("ui_snapshot", json!({})).await.unwrap(),
        json!({"request":1})
    );
    assert_eq!(
        client.call("ui_snapshot", json!({})).await.unwrap(),
        json!({"request":2})
    );
    server.await.unwrap();
}

#[tokio::test]
async fn canceled_mutation_drops_connection_and_is_not_replayed() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let (seen_tx, seen_rx) = oneshot::channel();
    let server = tokio::spawn(async move {
        let (first, _) = listener.accept().await.unwrap();
        let mut first = BufReader::new(first);
        let mut request_line = String::new();
        first.read_line(&mut request_line).await.unwrap();
        let first_request: Value = serde_json::from_str(&request_line).unwrap();
        assert_eq!(first_request["id"], 1);
        seen_tx.send(()).unwrap();

        let (second, _) = listener.accept().await.unwrap();
        let mut second = BufReader::new(second);
        let mut request_line = String::new();
        second.read_line(&mut request_line).await.unwrap();
        let second_request: Value = serde_json::from_str(&request_line).unwrap();
        assert_eq!(second_request["id"], 2);
        assert_eq!(second_request["method"], "ui_snapshot");
        let response = json!({"jsonrpc":"2.0","id":2,"result":{"ok":true}});
        second
            .get_mut()
            .write_all(format!("{response}\n").as_bytes())
            .await
            .unwrap();
    });
    let client = ashnazg::GuestClient::new(addr, "test-token".into());
    let first = tokio::spawn({
        let client = client.clone();
        async move { client.call("ui_act", json!({"action":"tap"})).await }
    });
    seen_rx.await.unwrap();
    first.abort();
    let result = client.call("ui_snapshot", json!({})).await.unwrap();
    assert_eq!(result, json!({"ok":true}));
    server.await.unwrap();
}

#[tokio::test]
async fn guest_client_rejects_response_with_result_and_error() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut stream = BufReader::new(stream);
        let mut line = String::new();
        stream.read_line(&mut line).await.unwrap();
        stream.get_mut().write_all(b"{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{},\"error\":{\"code\":-1,\"message\":\"bad\"}}\n").await.unwrap();
    });
    let client = ashnazg::GuestClient::new(addr, "test-token".into());
    assert!(matches!(
        client.call("ui_snapshot", json!({})).await,
        Err(ashnazg::GuestError::Malformed)
    ));
    server.await.unwrap();
}
