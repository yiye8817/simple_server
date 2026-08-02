import socket
import unittest

from dev_control.scan_fallback import scan


class ScanFallbackTest(unittest.TestCase):
    def test_detects_open_tcp_port_without_nmap(self):
        server=socket.socket()
        server.bind(("127.0.0.1",0))
        server.listen(1)
        try:
            port=server.getsockname()[1]
            output,error=scan("127.0.0.1",custom_ports=str(port))
            self.assertEqual(error,"")
            self.assertIn(f'portid="{port}"',output)
            self.assertIn('state="open"',output)
        finally:
            server.close()

    def test_invalid_custom_ports_returns_error(self):
        output,error=scan("127.0.0.1",custom_ports="invalid")
        self.assertEqual(output,"")
        self.assertIn("有效",error)


if __name__ == "__main__":
    unittest.main()
