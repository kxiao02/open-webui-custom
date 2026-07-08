import io
import mimetypes
import os
import shutil
import json
import logging
import re
import tempfile
from abc import ABC, abstractmethod
from typing import Any, BinaryIO, Tuple, Dict

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError
except ImportError:
    boto3 = None
    Config = None
    ClientError = Exception

from open_webui.config import (
    S3_ACCESS_KEY_ID,
    S3_BUCKET_NAME,
    S3_ENDPOINT_URL,
    S3_KEY_PREFIX,
    S3_REGION_NAME,
    S3_SECRET_ACCESS_KEY,
    S3_USE_ACCELERATE_ENDPOINT,
    S3_ADDRESSING_STYLE,
    S3_ENABLE_TAGGING,
    GCS_BUCKET_NAME,
    GOOGLE_APPLICATION_CREDENTIALS_JSON,
    AZURE_STORAGE_ENDPOINT,
    AZURE_STORAGE_CONTAINER_NAME,
    AZURE_STORAGE_KEY,
    STORAGE_PROVIDER,
    UPLOAD_DIR,
)
from open_webui.constants import ERROR_MESSAGES

try:
    from google.cloud import storage
    from google.cloud.exceptions import GoogleCloudError, NotFound
except ImportError:
    storage = None
    GoogleCloudError = Exception
    NotFound = Exception

try:
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient
    from azure.core.exceptions import ResourceNotFoundError
except ImportError:
    DefaultAzureCredential = None
    BlobServiceClient = None
    ResourceNotFoundError = Exception

log = logging.getLogger(__name__)
TEMP_DOWNLOAD_PREFIX = "open-webui-s3-"


def _require_dependency(name: str, value):
    if value is None:
        raise RuntimeError(
            f"{name} is required for the configured storage provider. "
            "Install the full backend requirements or the provider-specific SDK."
        )
    return value


def is_ephemeral_storage_file(file_path: str | os.PathLike | None) -> bool:
    if not file_path:
        return False
    return os.path.basename(os.fspath(file_path)).startswith(TEMP_DOWNLOAD_PREFIX)


def cleanup_ephemeral_storage_file(file_path: str | os.PathLike | None) -> None:
    if not is_ephemeral_storage_file(file_path):
        return

    resolved_path = os.fspath(file_path)
    try:
        os.remove(resolved_path)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Failed to remove ephemeral storage file %s: %s", resolved_path, e)


class StorageFileNotFoundError(FileNotFoundError):
    """Raised when a storage object does not exist at the given path."""


class StorageProvider(ABC):
    @abstractmethod
    def get_file(self, file_path: str) -> str:
        pass

    @abstractmethod
    def get_file_metadata(self, file_path: str) -> Dict[str, Any]:
        """Return metadata dict with at least 'size_bytes' and 'content_type'.

        Raises StorageFileNotFoundError when the object does not exist.
        """
        pass

    @abstractmethod
    def upload_file(
        self, file: BinaryIO, filename: str, tags: Dict[str, str]
    ) -> Tuple[bytes, str]:
        pass

    @abstractmethod
    def delete_all_files(self) -> None:
        pass

    @abstractmethod
    def delete_file(self, file_path: str) -> None:
        pass


class LocalStorageProvider(StorageProvider):
    @staticmethod
    def upload_file(
        file: BinaryIO, filename: str, tags: Dict[str, str]
    ) -> Tuple[bytes, str]:
        contents = file.read()
        if not contents:
            raise ValueError(ERROR_MESSAGES.EMPTY_CONTENT)
        file_path = f"{UPLOAD_DIR}/{filename}"
        with open(file_path, "wb") as f:
            f.write(contents)
        return contents, file_path

    @staticmethod
    def get_file(file_path: str) -> str:
        """Handles downloading of the file from local storage."""
        return file_path

    @staticmethod
    def get_file_metadata(file_path: str) -> Dict[str, Any]:
        filename = file_path.split("/")[-1]
        resolved = os.path.join(UPLOAD_DIR, filename)
        if not os.path.isfile(resolved):
            raise StorageFileNotFoundError(f"Local file not found: {resolved}")
        stat = os.stat(resolved)
        content_type, _ = mimetypes.guess_type(resolved)
        return {
            "size_bytes": stat.st_size,
            "content_type": content_type or "application/octet-stream",
        }

    @staticmethod
    def delete_file(file_path: str) -> None:
        """Handles deletion of the file from local storage."""
        filename = file_path.split("/")[-1]
        file_path = f"{UPLOAD_DIR}/{filename}"
        if os.path.isfile(file_path):
            os.remove(file_path)
        else:
            log.warning(f"File {file_path} not found in local storage.")

    @staticmethod
    def delete_all_files() -> None:
        """Handles deletion of all files from local storage."""
        if os.path.exists(UPLOAD_DIR):
            for filename in os.listdir(UPLOAD_DIR):
                file_path = os.path.join(UPLOAD_DIR, filename)
                try:
                    if os.path.isfile(file_path) or os.path.islink(file_path):
                        os.unlink(file_path)  # Remove the file or link
                    elif os.path.isdir(file_path):
                        shutil.rmtree(file_path)  # Remove the directory
                except Exception as e:
                    log.exception(f"Failed to delete {file_path}. Reason: {e}")
        else:
            log.warning(f"Directory {UPLOAD_DIR} not found in local storage.")


class S3StorageProvider(StorageProvider):
    def __init__(self):
        _require_dependency("boto3", boto3)
        config_cls = _require_dependency("botocore", Config)
        config = config_cls(
            s3={
                "use_accelerate_endpoint": S3_USE_ACCELERATE_ENDPOINT,
                "addressing_style": S3_ADDRESSING_STYLE,
            },
            # KIT change - see https://github.com/boto/boto3/issues/4400#issuecomment-2600742103∆
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        )

        # If access key and secret are provided, use them for authentication
        if S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY:
            self.s3_client = boto3.client(
                "s3",
                region_name=S3_REGION_NAME,
                endpoint_url=S3_ENDPOINT_URL,
                aws_access_key_id=S3_ACCESS_KEY_ID,
                aws_secret_access_key=S3_SECRET_ACCESS_KEY,
                config=config,
            )
        else:
            # If no explicit credentials are provided, fall back to default AWS credentials
            # This supports workload identity (IAM roles for EC2, EKS, etc.)
            self.s3_client = boto3.client(
                "s3",
                region_name=S3_REGION_NAME,
                endpoint_url=S3_ENDPOINT_URL,
                config=config,
            )

        self.bucket_name = S3_BUCKET_NAME
        self.key_prefix = S3_KEY_PREFIX if S3_KEY_PREFIX else ""

    @staticmethod
    def sanitize_tag_value(s: str) -> str:
        """Only include S3 allowed characters."""
        return re.sub(r"[^a-zA-Z0-9 äöüÄÖÜß\+\-=\._:/@]", "", s)

    def upload_file(
        self, file: BinaryIO, filename: str, tags: Dict[str, str]
    ) -> Tuple[bytes, str]:
        """Handles uploading of the file to S3 storage."""
        contents = file.read()
        if not contents:
            raise ValueError(ERROR_MESSAGES.EMPTY_CONTENT)
        s3_key = os.path.join(self.key_prefix, filename)
        try:
            self.s3_client.upload_fileobj(
                io.BytesIO(contents), self.bucket_name, s3_key
            )
            if S3_ENABLE_TAGGING and tags:
                sanitized_tags = {
                    self.sanitize_tag_value(k): self.sanitize_tag_value(v)
                    for k, v in tags.items()
                }
                tagging = {
                    "TagSet": [
                        {"Key": k, "Value": v} for k, v in sanitized_tags.items()
                    ]
                }
                self.s3_client.put_object_tagging(
                    Bucket=self.bucket_name,
                    Key=s3_key,
                    Tagging=tagging,
                )
            return (contents, f"s3://{self.bucket_name}/{s3_key}")
        except ClientError as e:
            raise RuntimeError(f"Error uploading file to S3: {e}")

    def get_file(self, file_path: str) -> str:
        """Handles downloading of the file from S3 storage."""
        try:
            s3_key = self._extract_s3_key(file_path)
            local_file_path = self._get_temp_file_path(s3_key)
            self.s3_client.download_file(self.bucket_name, s3_key, local_file_path)
            return local_file_path
        except ClientError as e:
            raise RuntimeError(f"Error downloading file from S3: {e}")

    def get_file_metadata(self, file_path: str) -> Dict[str, Any]:
        s3_key = self._extract_s3_key(file_path)
        try:
            response = self.s3_client.head_object(
                Bucket=self.bucket_name, Key=s3_key
            )
            return {
                "size_bytes": response.get("ContentLength"),
                "content_type": response.get("ContentType"),
            }
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "404":
                raise StorageFileNotFoundError(f"S3 object not found: {file_path}")
            raise RuntimeError(f"Error getting S3 object metadata: {e}")

    def delete_file(self, file_path: str) -> None:
        """Handles deletion of the file from S3 storage."""
        try:
            s3_key = self._extract_s3_key(file_path)
            self.s3_client.delete_object(Bucket=self.bucket_name, Key=s3_key)
        except ClientError as e:
            raise RuntimeError(f"Error deleting file from S3: {e}")

    def delete_all_files(self) -> None:
        """Handles deletion of all files from S3 storage."""
        try:
            response = self.s3_client.list_objects_v2(Bucket=self.bucket_name)
            if "Contents" in response:
                for content in response["Contents"]:
                    # Skip objects that were not uploaded from open-webui in the first place
                    if not content["Key"].startswith(self.key_prefix):
                        continue

                    self.s3_client.delete_object(
                        Bucket=self.bucket_name, Key=content["Key"]
                    )
        except ClientError as e:
            raise RuntimeError(f"Error deleting all files from S3: {e}")

    # The s3 key is the name assigned to an object. It excludes the bucket name, but includes the internal path and the file name.
    def _extract_s3_key(self, full_file_path: str) -> str:
        return "/".join(full_file_path.split("//")[1].split("/")[1:])

    def _get_temp_file_path(self, s3_key: str) -> str:
        suffix = os.path.splitext(s3_key)[1]
        temp_file = tempfile.NamedTemporaryFile(
            prefix=TEMP_DOWNLOAD_PREFIX,
            suffix=suffix,
            delete=False,
        )
        temp_file.close()
        return temp_file.name


class GCSStorageProvider(StorageProvider):
    def __init__(self):
        storage_client = _require_dependency("google-cloud-storage", storage)
        self.bucket_name = GCS_BUCKET_NAME

        if GOOGLE_APPLICATION_CREDENTIALS_JSON:
            self.gcs_client = storage_client.Client.from_service_account_info(
                info=json.loads(GOOGLE_APPLICATION_CREDENTIALS_JSON)
            )
        else:
            # if no credentials json is provided, credentials will be picked up from the environment
            # if running on local environment, credentials would be user credentials
            # if running on a Compute Engine instance, credentials would be from Google Metadata server
            self.gcs_client = storage_client.Client()
        self.bucket = self.gcs_client.bucket(GCS_BUCKET_NAME)

    def upload_file(
        self, file: BinaryIO, filename: str, tags: Dict[str, str]
    ) -> Tuple[bytes, str]:
        """Handles uploading of the file to GCS storage."""
        contents, file_path = LocalStorageProvider.upload_file(file, filename, tags)
        try:
            blob = self.bucket.blob(filename)
            blob.upload_from_filename(file_path)
            return contents, "gs://" + self.bucket_name + "/" + filename
        except GoogleCloudError as e:
            raise RuntimeError(f"Error uploading file to GCS: {e}")

    def get_file(self, file_path: str) -> str:
        """Handles downloading of the file from GCS storage."""
        try:
            filename = file_path.removeprefix("gs://").split("/")[1]
            local_file_path = f"{UPLOAD_DIR}/{filename}"
            blob = self.bucket.get_blob(filename)
            blob.download_to_filename(local_file_path)

            return local_file_path
        except NotFound as e:
            raise RuntimeError(f"Error downloading file from GCS: {e}")

    def get_file_metadata(self, file_path: str) -> Dict[str, Any]:
        filename = file_path.removeprefix("gs://").split("/")[1]
        blob = self.bucket.get_blob(filename)
        if blob is None:
            raise StorageFileNotFoundError(f"GCS object not found: {file_path}")
        blob.reload()
        return {
            "size_bytes": blob.size,
            "content_type": blob.content_type,
        }

    def delete_file(self, file_path: str) -> None:
        """Handles deletion of the file from GCS storage."""
        try:
            filename = file_path.removeprefix("gs://").split("/")[1]
            blob = self.bucket.get_blob(filename)
            blob.delete()
        except NotFound as e:
            raise RuntimeError(f"Error deleting file from GCS: {e}")

        # Always delete from local storage
        LocalStorageProvider.delete_file(file_path)

    def delete_all_files(self) -> None:
        """Handles deletion of all files from GCS storage."""
        try:
            blobs = self.bucket.list_blobs()

            for blob in blobs:
                blob.delete()

        except NotFound as e:
            raise RuntimeError(f"Error deleting all files from GCS: {e}")

        # Always delete from local storage
        LocalStorageProvider.delete_all_files()


class AzureStorageProvider(StorageProvider):
    def __init__(self):
        blob_service_client = _require_dependency(
            "azure-storage-blob", BlobServiceClient
        )
        self.endpoint = AZURE_STORAGE_ENDPOINT
        self.container_name = AZURE_STORAGE_CONTAINER_NAME
        storage_key = AZURE_STORAGE_KEY

        if storage_key:
            # Configure using the Azure Storage Account Endpoint and Key
            self.blob_service_client = blob_service_client(
                account_url=self.endpoint, credential=storage_key
            )
        else:
            # Configure using the Azure Storage Account Endpoint and DefaultAzureCredential
            # If the key is not configured, then the DefaultAzureCredential will be used to support Managed Identity authentication
            credential = _require_dependency("azure-identity", DefaultAzureCredential)
            self.blob_service_client = blob_service_client(
                account_url=self.endpoint, credential=credential()
            )
        self.container_client = self.blob_service_client.get_container_client(
            self.container_name
        )

    def upload_file(
        self, file: BinaryIO, filename: str, tags: Dict[str, str]
    ) -> Tuple[bytes, str]:
        """Handles uploading of the file to Azure Blob Storage."""
        contents, file_path = LocalStorageProvider.upload_file(file, filename, tags)
        try:
            blob_client = self.container_client.get_blob_client(filename)
            blob_client.upload_blob(contents, overwrite=True)
            return contents, f"{self.endpoint}/{self.container_name}/{filename}"
        except Exception as e:
            raise RuntimeError(f"Error uploading file to Azure Blob Storage: {e}")

    def get_file(self, file_path: str) -> str:
        """Handles downloading of the file from Azure Blob Storage."""
        try:
            filename = file_path.split("/")[-1]
            local_file_path = f"{UPLOAD_DIR}/{filename}"
            blob_client = self.container_client.get_blob_client(filename)
            with open(local_file_path, "wb") as download_file:
                download_file.write(blob_client.download_blob().readall())
            return local_file_path
        except ResourceNotFoundError as e:
            raise RuntimeError(f"Error downloading file from Azure Blob Storage: {e}")

    def get_file_metadata(self, file_path: str) -> Dict[str, Any]:
        filename = file_path.split("/")[-1]
        blob_client = self.container_client.get_blob_client(filename)
        try:
            props = blob_client.get_blob_properties()
            return {
                "size_bytes": props.size,
                "content_type": props.content_settings.content_type,
            }
        except ResourceNotFoundError:
            raise StorageFileNotFoundError(f"Azure blob not found: {file_path}")

    def delete_file(self, file_path: str) -> None:
        """Handles deletion of the file from Azure Blob Storage."""
        try:
            filename = file_path.split("/")[-1]
            blob_client = self.container_client.get_blob_client(filename)
            blob_client.delete_blob()
        except ResourceNotFoundError as e:
            raise RuntimeError(f"Error deleting file from Azure Blob Storage: {e}")

        # Always delete from local storage
        LocalStorageProvider.delete_file(file_path)

    def delete_all_files(self) -> None:
        """Handles deletion of all files from Azure Blob Storage."""
        try:
            blobs = self.container_client.list_blobs()
            for blob in blobs:
                self.container_client.delete_blob(blob.name)
        except Exception as e:
            raise RuntimeError(f"Error deleting all files from Azure Blob Storage: {e}")

        # Always delete from local storage
        LocalStorageProvider.delete_all_files()


def get_storage_provider(storage_provider: str):
    if storage_provider == "local":
        Storage = LocalStorageProvider()
    elif storage_provider == "s3":
        Storage = S3StorageProvider()
    elif storage_provider == "gcs":
        Storage = GCSStorageProvider()
    elif storage_provider == "azure":
        Storage = AzureStorageProvider()
    else:
        raise RuntimeError(f"Unsupported storage provider: {storage_provider}")
    return Storage


Storage = get_storage_provider(STORAGE_PROVIDER)
