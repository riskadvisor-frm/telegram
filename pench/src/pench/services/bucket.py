import os
import base64
import json
import logging
from pathlib import Path
from typing import Optional

from google.cloud import storage
from google.oauth2 import service_account

logger = logging.getLogger(__name__)


def load_credentials_from_env() -> service_account.Credentials:
    """Get credentials from environment variables."""
    base_64_json_credentials = os.getenv("GCP_SERVICE_ACCOUNT")
    if not base_64_json_credentials:
        raise RuntimeError("The GCP_SERVICE_ACCOUNT environment variable is not set.")

    json_credentials = base64.b64decode(base_64_json_credentials).decode("utf-8")
    credentials_info = json.loads(json_credentials)
    return service_account.Credentials.from_service_account_info(
        credentials_info,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )


def create_gcp_bucket_client() -> storage.Client:
    """Create GCP bucket client."""
    credentials = load_credentials_from_env()
    return storage.Client(credentials=credentials)


class GCPBucketService:
    def __init__(self, bucket_name: Optional[str] = None):
        self.storage_client = create_gcp_bucket_client()
        self.bucket_name = bucket_name or os.getenv("GCP_BUCKET_NAME")
        if not self.bucket_name:
            raise RuntimeError("Bucket name must be provided or set via GCP_BUCKET_NAME env var.")
        self.bucket = self.storage_client.bucket(self.bucket_name)

        logger.info(f"GCP Bucket service initialized for bucket: {self.bucket_name}")

    def upload_screenshot(self, screenshot_path: str, blob_name: str) -> str:
        """Upload a screenshot to GCP bucket and return public URL."""
        try:
            file_extension = Path(screenshot_path).suffix
            blob_name = f"{blob_name}{file_extension}"

            blob = self.bucket.blob(blob_name)
            blob.upload_from_filename(screenshot_path)

            public_url = f"https://storage.googleapis.com/{self.bucket_name}/{blob_name}"

            logger.info(f"Screenshot uploaded to: {public_url}")
            return public_url

        except Exception as e:
            logger.error(f"Error uploading screenshot to GCP: {e}")
            raise

    def upload_gif(self, gif_path: str, blob_name: str) -> str:
        """Upload a GIF file to GCP bucket and return public URL."""
        try:
            file_extension = Path(gif_path).suffix
            blob_name = f"{blob_name}{file_extension}"

            blob = self.bucket.blob(blob_name)
            blob.upload_from_filename(gif_path)

            public_url = f"https://storage.googleapis.com/{self.bucket_name}/{blob_name}"

            logger.info(f"GIF uploaded to: {public_url}")
            return public_url

        except Exception as e:
            logger.error(f"Error uploading GIF to GCP: {e}")
            raise

    def upload_file(self, file_path: str, blob_name: str) -> str:
        """Upload any file to GCP bucket and return public URL."""
        try:
            file_extension = Path(file_path).suffix
            blob_name = f"{blob_name}{file_extension}"

            blob = self.bucket.blob(blob_name)
            blob.upload_from_filename(file_path)

            public_url = f"https://storage.googleapis.com/{self.bucket_name}/{blob_name}"

            logger.info(f"File uploaded to: {public_url}")
            return public_url

        except Exception as e:
            logger.error(f"Error uploading file to GCP: {e}")
            raise
