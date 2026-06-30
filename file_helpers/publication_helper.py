from io import BytesIO
import boto3
import pandas as pd
from configs import envs

class PublicationName():
    def __init__(self) -> None:
        self.publication_to_country_cache = None

    def load_publication_to_country(self):
        # Loads the publication-to-country mapping from an Excel file
        s3 = boto3.client('s3', region_name=envs.AWS_REGION, aws_access_key_id=envs.AWS_ACCESS_KEY_ID, aws_secret_access_key=envs.AWS_SECRET_ACCESS_KEY)
        response = s3.get_object(Bucket=envs.AWS_S3_REACH_BUCKET, Key=envs.PUBLICATION_SOURCE_FILE)
        df = pd.read_csv(
            BytesIO(response['Body'].read()),
            usecols=[1, 2],
            dtype={'PublicationUrl': str, 'PublicationName': str},
            low_memory=False
        )
        return dict(zip(df['PublicationUrl'], df['PublicationName']))

    def get_publication_name_for_domain(self, source_domain):
        # Uses the cached dictionary for fast lookups
        if self.publication_to_country_cache is None:
            self.publication_to_country_cache = self.load_publication_to_country()

        # Check if source_domain is in the precomputed dictionary
        if source_domain in self.publication_to_country_cache:
            return self.publication_to_country_cache[source_domain]
        else:
            return source_domain


publication_name = PublicationName()