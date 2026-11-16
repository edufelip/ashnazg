import json
import unittest
from mcp_http import decode_response, McpHttpClient, ProofFailure, structured

class ClientFixtureTests(unittest.TestCase):
    def test_json_and_single_sse_response(self):
        expected = {"jsonrpc":"2.0","id":1,"result":{"ok":True}}
        raw = json.dumps(expected).encode()
        self.assertEqual(decode_response(raw), expected)
        self.assertEqual(decode_response(b"event: message\r\ndata: " + raw + b"\r\n\r\n"), expected)
        self.assertEqual(decode_response(b"data: \nid: 0\nretry: 3000\n\ndata: " + raw + b"\n\n"), expected)
        self.assertIsNone(decode_response(b""))
        for bad in [b"[]", b"not json", b"data: {}\n\ndata: {}\n\n"]:
            with self.assertRaises(ProofFailure): decode_response(bad)

    def test_only_owned_loopback_address_and_private_repr(self):
        client = McpHttpClient("127.0.0.1:17779", "private-sentinel")
        self.assertNotIn("private-sentinel", repr(client))
        for address in ["example.com:80", "0.0.0.0:17779", "127.0.0.1:0", "127.0.0.1:65536", "127.0.0.1:80/path"]:
            with self.assertRaises(ProofFailure): McpHttpClient(address, "private")

    def test_requires_structured_tool_content(self):
        self.assertEqual(structured({"result":{"structuredContent":{"installed":True}}}), {"installed":True})
        with self.assertRaises(ProofFailure): structured({"result":{"content":[]}})

if __name__ == "__main__": unittest.main()
