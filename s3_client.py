"""
s3_client.py
------------
Thin wrapper around boto3 for recording storage on AWS S3.

Responsibilities:
- Lazy singleton boto3 client (initialised on first use, not at import time).
- `generate_presigned_url(s3_key)` — returns a time-limited playback URL.
- `delete_object(s3_key)` — used if a recording row is deleted.

LiveKit egress writes directly to S3 using the credentials we pass in the
EgressRequest, so we do NOT need to upload files here — boto3 is only used
for generating pre-signed GET URLs and optional cleanup.
"""

import boto3
from botocore.exceptions import ClientError
from config import settings

_s3_client = None


def get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client(
            "s3",
            region_name=settings.AWS_REGION,
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            endpoint_url=f"https://s3.{settings.AWS_REGION}.amazonaws.com",  # ← add this
        )
    return _s3_client


def generate_presigned_url(s3_key: str, expiry: int = None) -> str:
    """
    Generates a pre-signed S3 GET URL for the given object key.

    Args:
        s3_key:  S3 object key, e.g. "recordings/classroom_abc123/rec_xyz.mp4"
        expiry:  URL lifetime in seconds; defaults to AWS_S3_PRESIGNED_URL_EXPIRY
                 from settings (typically 3600 = 1 hour).

    Returns:
        Fully-signed HTTPS URL string valid for `expiry` seconds.

    Raises:
        ClientError: if the object doesn't exist or credentials are invalid.
    """
    expiry = expiry or settings.AWS_S3_PRESIGNED_URL_EXPIRY
    client = get_s3_client()
    url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.AWS_S3_BUCKET_NAME, "Key": s3_key},
        ExpiresIn=expiry,
    )
    return url


def delete_object(s3_key: str) -> None:
    """
    Deletes an S3 object. Silent no-op if the object doesn't exist.

    Args:
        s3_key: S3 object key to delete.
    """
    try:
        client = get_s3_client()
        client.delete_object(Bucket=settings.AWS_S3_BUCKET_NAME, Key=s3_key)
    except ClientError:
        pass