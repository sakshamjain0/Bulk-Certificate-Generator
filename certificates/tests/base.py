import logging
import shutil
import tempfile

from django.test import override_settings
from rest_framework.test import APITestCase


def make_payload(recipients=None, **overrides):
    payload = {
        "course_name": "Advanced Django",
        "issuer_name": "Acme Academy",
        "issue_date": "2026-10-01",
        "recipients": recipients if recipients is not None else [
            {"name": "Alice Smith", "email": "alice@example.com"},
            {"name": "Bob Jones", "email": "bob@example.com"},
        ],
    }
    payload.update(overrides)
    return payload


class CertificateAPITestCase(APITestCase):
    """Runs jobs synchronously and writes files to a throwaway MEDIA_ROOT."""

    @classmethod
    def setUpClass(cls):
        cls._media_root = tempfile.mkdtemp(prefix="cert-test-media-")
        cls._settings = override_settings(MEDIA_ROOT=cls._media_root, CERTIFICATE_PROCESSING_MODE="sync")
        cls._settings.enable()
        logging.disable(logging.CRITICAL)  # failures are expected in tests; keep output clean
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        logging.disable(logging.NOTSET)
        cls._settings.disable()
        shutil.rmtree(cls._media_root, ignore_errors=True)

    def create_job(self, payload=None, query=""):
        return self.client.post(f"/api/jobs/{query}", payload or make_payload(), format="json")

    @staticmethod
    def read_file_response(response) -> bytes:
        if hasattr(response, "streaming_content"):
            try:
                return b"".join(response.streaming_content)
            finally:
                response.close()
        return response.content
