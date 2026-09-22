"""Unsplash 图片服务的容错与密钥保护测试。"""

import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from app.services.unsplash_service import UnsplashService


class UnsplashServiceTest(unittest.TestCase):
    def create_service(self, access_key: str = "test-secret") -> UnsplashService:
        with patch(
            "app.services.unsplash_service.get_settings",
            return_value=SimpleNamespace(unsplash_access_key=access_key),
        ):
            return UnsplashService()

    def test_uses_authorization_header_instead_of_query_parameter(self):
        service = self.create_service()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "results": [
                {
                    "id": "photo-1",
                    "urls": {"regular": "https://images.example/photo.jpg"},
                    "user": {"name": "摄影师"},
                }
            ]
        }
        service.session.get = Mock(return_value=response)

        photos = service.search_photos("上海地标", per_page=1)

        self.assertEqual(photos[0]["id"], "photo-1")
        _, kwargs = service.session.get.call_args
        self.assertNotIn("client_id", kwargs["params"])
        self.assertEqual(kwargs["headers"]["Authorization"], "Client-ID test-secret")

    def test_ssl_failure_does_not_log_secret_and_temporarily_stops_requests(self):
        service = self.create_service()
        service.session.get = Mock(
            side_effect=requests.exceptions.SSLError(
                "https://api.unsplash.com/search/photos?client_id=test-secret"
            )
        )
        output = io.StringIO()

        with redirect_stdout(output):
            self.assertEqual(service.search_photos("上海地标"), [])
            self.assertEqual(service.search_photos("上海"), [])

        self.assertEqual(service.session.get.call_count, 1)
        self.assertNotIn("test-secret", output.getvalue())


if __name__ == "__main__":
    unittest.main()
