import os
import boto3
from botocore.exceptions import ClientError
import shutil


from typing import BinaryIO, Tuple, Optional, Union

from selfai_ui.constants import ERROR_MESSAGES
from selfai_ui.config import (
    STORAGE_PROVIDER,
    S3_ACCESS_KEY_ID,
    S3_SECRET_ACCESS_KEY,
    S3_BUCKET_NAME,
    S3_REGION_NAME,
    S3_ENDPOINT_URL,
    UPLOAD_DIR,
)


import boto3
from botocore.exceptions import ClientError
from typing import BinaryIO, Tuple, Optional


class StorageProvider:
    def __init__(self, provider: Optional[str] = None):
        self.storage_provider: str = provider or STORAGE_PROVIDER

        self.s3_client = None
        self.s3_bucket_name: Optional[str] = None

        if self.storage_provider == "s3":
            self._initialize_s3()

    def _initialize_s3(self) -> None:
        """Initializes the S3 client and bucket name if using S3 storage."""
        self.s3_client = boto3.client(
            "s3",
            region_name=S3_REGION_NAME,
            endpoint_url=S3_ENDPOINT_URL,
            aws_access_key_id=S3_ACCESS_KEY_ID,
            aws_secret_access_key=S3_SECRET_ACCESS_KEY,
        )
        self.bucket_name = S3_BUCKET_NAME

    def _upload_to_s3(self, file_path: str, filename: str) -> Tuple[bytes, str]:
        """Handles uploading of the file to S3 storage."""
        if not self.s3_client:
            raise RuntimeError("S3 Client is not initialized.")

        try:
            self.s3_client.upload_file(file_path, self.bucket_name, filename)
            return (
                open(file_path, "rb").read(),
                "s3://" + self.bucket_name + "/" + filename,
            )
        except ClientError as e:
            raise RuntimeError(f"Error uploading file to S3: {e}")

    def _upload_to_local(self, contents: bytes, filename: str, subdirectory: str = None) -> Tuple[bytes, str]:
        """Handles uploading of the file to local storage."""
        if subdirectory:
            target_dir = f"{UPLOAD_DIR}/{subdirectory}"
            os.makedirs(target_dir, exist_ok=True)
            file_path = f"{target_dir}/{filename}"
        else:
            file_path = f"{UPLOAD_DIR}/{filename}"
        with open(file_path, "wb") as f:
            f.write(contents)
        return contents, file_path

    def _get_file_from_s3(self, file_path: str) -> str:
        """Handles downloading of the file from S3 storage."""
        if not self.s3_client:
            raise RuntimeError("S3 Client is not initialized.")

        try:
            bucket_name, key = file_path.split("//")[1].split("/")
            local_file_path = f"{UPLOAD_DIR}/{key}"
            self.s3_client.download_file(bucket_name, key, local_file_path)
            return local_file_path
        except ClientError as e:
            raise RuntimeError(f"Error downloading file from S3: {e}")

    def _get_file_from_local(self, file_path: str) -> str:
        """Handles downloading of the file from local storage."""
        return file_path

    def _delete_from_s3(self, filename: str) -> None:
        """Handles deletion of the file from S3 storage."""
        if not self.s3_client:
            raise RuntimeError("S3 Client is not initialized.")

        try:
            self.s3_client.delete_object(Bucket=self.bucket_name, Key=filename)
        except ClientError as e:
            raise RuntimeError(f"Error deleting file from S3: {e}")

    def _delete_from_local(self, file_path: str) -> None:
        """Handles deletion of the file from local storage."""
        if os.path.isfile(file_path):
            os.remove(file_path)
            # Clean up empty parent directory if it's a KB subdirectory
            parent = os.path.dirname(file_path)
            if parent != UPLOAD_DIR and os.path.isdir(parent) and not os.listdir(parent):
                os.rmdir(parent)
        else:
            print(f"File {file_path} not found in local storage.")

    def _delete_all_from_s3(self) -> None:
        """Handles deletion of all files from S3 storage."""
        if not self.s3_client:
            raise RuntimeError("S3 Client is not initialized.")

        try:
            response = self.s3_client.list_objects_v2(Bucket=self.bucket_name)
            if "Contents" in response:
                for content in response["Contents"]:
                    self.s3_client.delete_object(
                        Bucket=self.bucket_name, Key=content["Key"]
                    )
        except ClientError as e:
            raise RuntimeError(f"Error deleting all files from S3: {e}")

    def _delete_all_from_local(self) -> None:
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
                    print(f"Failed to delete {file_path}. Reason: {e}")
        else:
            print(f"Directory {UPLOAD_DIR} not found in local storage.")

    def upload_file(self, file: BinaryIO, filename: str, subdirectory: str = None) -> Tuple[bytes, str]:
        """Uploads a file either to S3 or the local file system."""
        contents = file.read()
        if not contents:
            raise ValueError(ERROR_MESSAGES.EMPTY_CONTENT)
        contents, file_path = self._upload_to_local(contents, filename, subdirectory)

        if self.storage_provider == "s3":
            s3_key = f"{subdirectory}/{filename}" if subdirectory else filename
            return self._upload_to_s3(file_path, s3_key)
        return contents, file_path

    def get_file(self, file_path: str) -> str:
        """Downloads a file either from S3 or the local file system and returns the file path."""
        if self.storage_provider == "s3":
            return self._get_file_from_s3(file_path)
        return self._get_file_from_local(file_path)

    def delete_file(self, file_path: str) -> None:
        """Deletes a file either from S3 or the local file system."""
        if self.storage_provider == "s3":
            # Extract S3 key from s3://bucket/key path
            if file_path.startswith("s3://"):
                key = "/".join(file_path.split("//")[1].split("/")[1:])
            else:
                key = file_path.split("/")[-1]
            self._delete_from_s3(key)

        # Always delete from local storage
        if file_path.startswith("s3://"):
            # For S3, reconstruct local path from the key
            key = "/".join(file_path.split("//")[1].split("/")[1:])
            local_path = f"{UPLOAD_DIR}/{key}"
        else:
            local_path = file_path
        self._delete_from_local(local_path)

    def move_file(self, current_path: str, new_subdirectory: str) -> str:
        """Moves a file to a new subdirectory within the upload directory."""
        basename = os.path.basename(current_path)
        target_dir = f"{UPLOAD_DIR}/{new_subdirectory}"
        new_path = f"{target_dir}/{basename}"

        if current_path == new_path:
            return new_path

        os.makedirs(target_dir, exist_ok=True)
        shutil.move(current_path, new_path)

        # Clean up empty source directory
        source_dir = os.path.dirname(current_path)
        if source_dir != UPLOAD_DIR and os.path.isdir(source_dir) and not os.listdir(source_dir):
            os.rmdir(source_dir)

        if self.storage_provider == "s3":
            old_key = os.path.basename(current_path)
            new_key = f"{new_subdirectory}/{basename}"
            try:
                self.s3_client.copy_object(
                    Bucket=self.bucket_name,
                    CopySource={"Bucket": self.bucket_name, "Key": old_key},
                    Key=new_key,
                )
                self._delete_from_s3(old_key)
                return f"s3://{self.bucket_name}/{new_key}"
            except ClientError as e:
                raise RuntimeError(f"Error moving file in S3: {e}")

        return new_path

    def delete_subdirectory(self, subdirectory: str) -> None:
        """Deletes an entire subdirectory from storage."""
        local_dir = f"{UPLOAD_DIR}/{subdirectory}"
        if os.path.isdir(local_dir):
            shutil.rmtree(local_dir, ignore_errors=True)

        if self.storage_provider == "s3" and self.s3_client:
            try:
                response = self.s3_client.list_objects_v2(
                    Bucket=self.bucket_name, Prefix=f"{subdirectory}/"
                )
                if "Contents" in response:
                    for content in response["Contents"]:
                        self.s3_client.delete_object(
                            Bucket=self.bucket_name, Key=content["Key"]
                        )
            except ClientError as e:
                print(f"Error deleting subdirectory from S3: {e}")

    def delete_all_files(self) -> None:
        """Deletes all files from the storage."""
        if self.storage_provider == "s3":
            self._delete_all_from_s3()

        # Always delete from local storage
        self._delete_all_from_local()


Storage = StorageProvider(provider=STORAGE_PROVIDER)
