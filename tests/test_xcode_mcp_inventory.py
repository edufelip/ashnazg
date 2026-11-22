import unittest
from unittest.mock import Mock

from scripts.xcode_mcp_inventory import list_tools


class XcodeMcpInventoryTest(unittest.TestCase):
    def test_initializes_and_retains_all_tool_pages(self):
        client = Mock()
        client.request.side_effect = [
            {"id": 1, "result": {"protocolVersion": "2025-03-26", "serverInfo": {"name": "Xcode"}}},
            {"id": 2, "result": {"tools": [{"name": "a"}], "nextCursor": "second"}},
            {"id": 3, "result": {"tools": [{"name": "b"}]}},
        ]
        result = list_tools(client)
        self.assertEqual([item["name"] for item in result["tools"]], ["a", "b"])
        self.assertEqual(client.request.call_args.args[0]["params"], {"cursor": "second"})
        client.notification.assert_called_once_with({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def test_rejects_malformed_or_repeated_cursor(self):
        client = Mock()
        client.request.side_effect = [
            {"id": 1, "result": {"protocolVersion": "2025-03-26"}},
            {"id": 2, "result": {"tools": [], "nextCursor": "loop"}},
            {"id": 3, "result": {"tools": [], "nextCursor": "loop"}},
        ]
        with self.assertRaisesRegex(RuntimeError, "cursor"):
            list_tools(client)
