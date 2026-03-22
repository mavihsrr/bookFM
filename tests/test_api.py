import unittest

from fastapi.testclient import TestClient

from bookfm.api import app


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_health_endpoint_reports_capabilities(self) -> None:
        response = self.client.get("/v1/health")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertIn("uploads_available", payload)
        self.assertIn("ui_available", payload)

    def test_root_serves_ui(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("BookFM", response.text)

    def test_room_serves_ui(self) -> None:
        response = self.client.get("/room")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Open reading room", response.text)

    def test_stream_socket_rejects_empty_text(self) -> None:
        with self.client.websocket_connect("/v1/stream/live") as websocket:
            websocket.send_json({"text": ""})
            payload = websocket.receive_json()
            self.assertEqual(payload["event"], "error")
            self.assertIn("Text is required", payload["detail"])


if __name__ == "__main__":
    unittest.main()
