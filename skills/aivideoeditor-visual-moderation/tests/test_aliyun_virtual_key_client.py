import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_aliyun_video_moderation.py"
SPEC = importlib.util.spec_from_file_location("run_aliyun_video_moderation", SCRIPT_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeResponse:
    status = 200

    def __init__(self, body):
        self._body = body

    def read(self):
        return json.dumps(self._body).encode("utf-8")


class FakeConnection:
    instances = []
    response_body = {"taskId": "green-task-1", "submitted": {"code": 200}}

    def __init__(self, host, port, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.method = ""
        self.target = ""
        self.headers = {}
        self.payload = bytearray()
        self.__class__.instances.append(self)

    def putrequest(self, method, target):
        self.method = method
        self.target = target

    def putheader(self, name, value):
        self.headers[name] = value

    def endheaders(self):
        return None

    def send(self, payload):
        self.payload.extend(payload)

    def getresponse(self):
        return FakeResponse(self.response_body)

    def close(self):
        return None


class AliyunVirtualKeyClientTests(unittest.TestCase):
    def setUp(self):
        FakeConnection.instances.clear()
        FakeConnection.response_body = {"taskId": "green-task-1", "submitted": {"code": 200}}

    def test_local_upload_sends_virtual_key_only_to_runtime_proxy(self):
        client = MODULE.AliyunGreenVirtualKeyClient(
            "vk_unit_test",
            "https://platform.example/api/v1/api-key-distribution/runtime/aliyun-green",
            10,
        )
        with tempfile.TemporaryDirectory() as temporary_dir:
            video_path = Path(temporary_dir) / "review.mp4"
            video_path.write_bytes(b"video-bytes")
            with patch.object(MODULE.http.client, "HTTPSConnection", FakeConnection):
                response = client.submit_local(str(video_path), "videoDetection")

        connection = FakeConnection.instances[0]
        self.assertEqual(response["taskId"], "green-task-1")
        self.assertEqual(connection.method, "POST")
        self.assertIn("video-moderation/local", connection.target)
        self.assertIn("fileName=review.mp4", connection.target)
        self.assertEqual(connection.headers["Authorization"], "Bearer vk_unit_test")
        self.assertEqual(bytes(connection.payload), b"video-bytes")

    def test_result_query_returns_underlying_green_result(self):
        FakeConnection.response_body = {"result": {"code": 200, "data": {"taskId": "green-task-1"}}}
        client = MODULE.AliyunGreenVirtualKeyClient(
            "vk_unit_test",
            "https://platform.example/api/v1/api-key-distribution/runtime/aliyun-green",
            10,
        )
        with patch.object(MODULE.http.client, "HTTPSConnection", FakeConnection):
            response = client.query("green-task-1", "videoDetection")

        self.assertEqual(response["data"]["taskId"], "green-task-1")
