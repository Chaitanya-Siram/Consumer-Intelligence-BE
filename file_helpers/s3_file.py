import boto3
from configs import envs, logger

class S3File:
    def __init__(self):
        pass

    def download_file(self, file_key: str) -> bytes:
        """Download a file from S3 and return its content as bytes."""
        s3 = boto3.client(
            's3',
            aws_access_key_id=envs.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=envs.AWS_SECRET_ACCESS_KEY,
            region_name=envs.AWS_REGION
        )
        response = s3.get_object(Bucket=envs.AWS_S3_BUCKET, Key=file_key)
        return response['Body'].read()
    
    def upload_file(self, file_key: str, file_content: bytes):
        """Upload a file to S3 from bytes content."""
        s3 = boto3.client(
            's3',
            aws_access_key_id=envs.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=envs.AWS_SECRET_ACCESS_KEY,
            region_name=envs.AWS_REGION
        )
        s3.put_object(Bucket=envs.AWS_S3_BUCKET, Key=file_key, Body=file_content)
        logger.info(f"Uploaded file to S3 bucket '{envs.AWS_S3_BUCKET}' with key '{file_key}'")

    def delete_file(self, file_key: str):
        """Delete a file from S3 by its key."""
        s3 = boto3.client(
            's3',
            aws_access_key_id=envs.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=envs.AWS_SECRET_ACCESS_KEY,
            region_name=envs.AWS_REGION
        )
        s3.delete_object(Bucket=envs.AWS_S3_BUCKET, Key=file_key)
        logger.info(f"Deleted file from S3 bucket '{envs.AWS_S3_BUCKET}' with key '{file_key}'")


s3_file = S3File()