"""HTTP transport initialization and browser cookie import, without network I/O."""

import unittest

import requests

from guet.config import RuntimeConfig
from guet.transport import HttpTransport


class HttpTransportInitializationTest(unittest.TestCase):
    def test_headers_initialized_for_all_cookie_formats(self):
        cookie_list = [{
            "name": "session", "value": "test-session",
            "domain": "courses.example.test", "path": "/api", "secure": True,
        }]
        for cookies in (None, {}, {"session": "test-session"}, cookie_list):
            with self.subTest(cookies=cookies):
                transport = HttpTransport(RuntimeConfig(user_agent="review-test-agent"), cookies)
                self.addCleanup(transport.close)
                self.assertEqual(transport.session.headers["User-Agent"], "review-test-agent")
                self.assertEqual(transport.session.headers["Accept-Language"], "zh-CN,zh;q=0.9")

    def test_browser_cookie_keeps_domain_path_and_secure_scope(self):
        transport = HttpTransport(RuntimeConfig(), [{
            "name": "session", "value": "test-session",
            "domain": "courses.example.test", "path": "/api", "secure": True,
        }])
        self.addCleanup(transport.close)
        for url, expected in (
            ("https://courses.example.test/api/courses", True),
            ("https://unrelated.example.test/api/courses", False),
            ("https://courses.example.test/other", False),
            ("http://courses.example.test/api/courses", False),
        ):
            with self.subTest(url=url):
                prepared = transport.session.prepare_request(requests.Request("GET", url))
                self.assertEqual("session=test-session" in prepared.headers.get("Cookie", ""), expected)


if __name__ == "__main__":
    unittest.main()
