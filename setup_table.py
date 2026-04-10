#!/usr/bin/env python3
"""Create the DynamoDB table Dave uses for state."""
import sys

from config import load_config
from state import _build_boto3_session


def create_table(config):
    session = _build_boto3_session(
        aws_profile=config.aws_profile,
        aws_region=config.aws_region,
        aws_access_key_id=config.aws_access_key_id,
        aws_secret_access_key=config.aws_secret_access_key,
    )
    ddb = session.client("dynamodb")

    try:
        ddb.create_table(
            TableName=config.dynamodb_table,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        print(f"Created table: {config.dynamodb_table}")
        print("Waiting for table to be active...")
        waiter = ddb.get_waiter("table_exists")
        waiter.wait(TableName=config.dynamodb_table)
        print("Table is ready.")
    except ddb.exceptions.ResourceInUseException:
        print(f"Table '{config.dynamodb_table}' already exists.")


if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else "dave.yaml"
    config = load_config(config_path)
    create_table(config)
