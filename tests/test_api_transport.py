"""Exercise real request-body writes against a local slow receiver; no paid API."""
import base64
import importlib.util
import json
import ipaddress
import socket
import ssl
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import requests
from urllib3.connection import HTTPConnection
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


spec = importlib.util.spec_from_file_location("ziyuan_transport_api", Path(__file__).resolve().parents[1] / "api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


class SlowReceiver(BaseHTTPRequestHandler):
    def do_POST(self):
        self.server.calls.append(self.path)
        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 256 * 1024)
        self.server.release.wait(25)
        remaining = int(self.headers["Content-Length"])
        try:
            while remaining:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    return
                remaining -= len(chunk)
            self.server.received.append(int(self.headers["Content-Length"]))
            body = json.dumps({"data": [{"url": "https://example.test/output.png"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionError, OSError):
            pass  # The timeout regression intentionally disconnects mid-upload.

    def log_message(self, *args):
        pass


class TransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="ziyuan-transport-")
        cls.addClassCleanup(cls.directory.cleanup)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
                .sign(key, hashes.SHA256()))
        cls.cert_path = Path(cls.directory.name) / "cert.pem"
        key_path = Path(cls.directory.name) / "key.pem"
        cls.cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
        cls.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.tls.load_cert_chain(cls.cert_path, key_path)

    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SlowReceiver, bind_and_activate=False)
        self.server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 * 1024)
        self.server.server_bind()
        self.server.server_activate()
        self.server.socket = self.tls.wrap_socket(self.server.socket, server_side=True)
        self.server.calls = []
        self.server.received = []
        self.server.release = threading.Event()
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base = f"https://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.stop_server)
        socket_options = HTTPConnection.default_socket_options + [(socket.SOL_SOCKET, socket.SO_SNDBUF, 64 * 1024)]
        self.socket_patch = patch.object(HTTPConnection, "default_socket_options", socket_options)
        self.socket_patch.start()
        self.addCleanup(self.socket_patch.stop)

    def stop_server(self):
        self.server.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()

    def upload(self, model, timeout=30):
        client = api.ZiyuanClient(self.base, "test-key", timeout)
        client.session.verify = str(self.cert_path)
        try:
            source = "data:image/png;base64," + base64.b64encode(b"x" * (16 * 1024 * 1024)).decode()
            return client.generate("image", {"model": model, "image": [source], "prompt": "test"})
        finally:
            client.close()

    def test_json_and_multipart_upload_can_take_more_than_fifteen_seconds(self):
        # Pause the receiver past the former 15-second write timeout. The body is
        # larger than TCP buffers, so success requires waiting during transmission.
        timer = threading.Timer(17, self.server.release.set)
        timer.start()
        self.addCleanup(timer.cancel)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.upload, model) for model in ("gpt-image-2", "gpt-image-2-super")]
            for future in futures:
                with self.subTest(request=futures.index(future)):
                    self.assertEqual(future.result(timeout=35)[0], ["https://example.test/output.png"])
        self.assertEqual(len(self.server.calls), 2)
        self.assertEqual(len(self.server.received), 2)
        self.assertTrue(all(size > 16 * 1024 * 1024 for size in self.server.received))

    def test_upload_still_stops_at_node_timeout_without_resubmitting(self):
        started = time.monotonic()
        with self.assertRaises((TimeoutError, RuntimeError)) as error:
            self.upload("gpt-image-2", timeout=0.3)
        self.assertIn("请求没有自动重发", str(error.exception))
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(len(self.server.calls), 1)

    def test_wrapped_write_timeout_has_readable_message_and_never_retries(self):
        client = api.ZiyuanClient(self.base, "test-key", 30)
        self.addCleanup(client.close)
        error = requests.ConnectionError("Connection aborted.", TimeoutError("The write operation timed out"))
        with patch.object(client.session, "request", side_effect=error) as request:
            with self.assertRaisesRegex(RuntimeError, "连接.*请求没有自动重发.*网站任务记录"):
                client.generate("image", {"model": "gpt-image-2", "prompt": "test"})
        self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
