from typing import Dict, NamedTuple
from aws_cdk import (
    Duration,
    Stack,
    aws_apigateway as apigw,
    aws_sqs as sqs,
    aws_lambda as lambda_,
    aws_dynamodb as dynamodb,
)
from constructs import Construct
from enum import Enum


class LambdaQueueTuple(NamedTuple):
    lambda_function: lambda_.Function
    queue: sqs.Queue


class DynamoPermission(Enum):
    READ = "READ"
    WRITE = "WRITE"
    READ_WRITE = "READ_WRITE"


_READ = DynamoPermission.READ
_WRITE = DynamoPermission.WRITE
_READ_WRITE = DynamoPermission.READ_WRITE


class TablePermissionTuple(NamedTuple):
    table: dynamodb.Table
    permission: DynamoPermission


class NdMainStack(Stack):
    def __init__(
        self, scope: Construct, construct_id: str, prefix: str, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.prefix = prefix

        # DynamoDB Table imports
        self.users: dynamodb.Table = self.import_dynamodb_table("Users")
        self.apis: dynamodb.Table = self.import_dynamodb_table("APIs")
        self.sessions: dynamodb.Table = self.import_dynamodb_table("Sessions")
        self.models: dynamodb.Table = self.import_dynamodb_table("Models")
        self.usage_logs: dynamodb.Table = self.import_dynamodb_table("UsageLogs")

        # API Gateway
        self.apigw_resources: Dict[str, apigw.Resource] = {}
        self.api = apigw.RestApi(self, f"{prefix}_api")
        POST_signup = self.add(
            "POST",
            "signup",
            tables=[TablePermissionTuple(self.users, _READ_WRITE)],
            create_queue=True,
        )
        POST_signin = self.add(
            "POST",
            "signin",
            tables=[
                TablePermissionTuple(self.users, _READ),
                TablePermissionTuple(self.sessions, _READ_WRITE),
            ],
        )
        GET_access_token = self.add(
            "GET",
            "access_token",
            tables=[
                TablePermissionTuple(self.users, _READ_WRITE),
                TablePermissionTuple(self.sessions, _READ_WRITE),
                TablePermissionTuple(self.apis, _READ_WRITE),
                TablePermissionTuple(self.models, _READ_WRITE),
                TablePermissionTuple(self.usage_logs, _READ_WRITE),
            ],
        )
        POST_create_endpoint = self.add(
            "POST",
            "create_endpoint",
            tables=[
                TablePermissionTuple(self.users, _READ_WRITE),
                TablePermissionTuple(self.sessions, _READ_WRITE),
                TablePermissionTuple(self.apis, _READ_WRITE),
                TablePermissionTuple(self.models, _READ_WRITE),
                TablePermissionTuple(self.usage_logs, _READ_WRITE),
            ],
        )
        POST_associate_ml_model = self.add(
            "POST",
            "associate_ml_model",
            tables=[
                TablePermissionTuple(self.users, _READ_WRITE),
                TablePermissionTuple(self.sessions, _READ_WRITE),
                TablePermissionTuple(self.apis, _READ_WRITE),
                TablePermissionTuple(self.models, _READ_WRITE),
                TablePermissionTuple(self.usage_logs, _READ_WRITE),
            ],
        )
        GET_ml_model_upload = self.add(
            "GET",
            "ml_model_upload",
            tables=[
                TablePermissionTuple(self.users, _READ_WRITE),
                TablePermissionTuple(self.sessions, _READ_WRITE),
                TablePermissionTuple(self.apis, _READ_WRITE),
                TablePermissionTuple(self.models, _READ_WRITE),
                TablePermissionTuple(self.usage_logs, _READ_WRITE),
            ],
        )

    def create_lambda(
        self,
        id: str,
        tables: list[TablePermissionTuple],
        queue: sqs.Queue = None,
    ) -> lambda_.Function:
        # environment variables for the lambda function
        env = {table.table_name: table.table_arn for (table, _) in tables}
        if queue:
            env["queue"] = queue.queue_url

        # create lambda function
        _lambda = lambda_.Function(
            self,
            id,
            function_name=f"{self.prefix}_{id}",
            runtime=lambda_.Runtime.PYTHON_3_9,
            code=lambda_.Code.from_asset("src"),
            handler=f"{id}.handler",
            environment=env,
            timeout=Duration.seconds(29),
        )

        # grant lambda function access to DynamoDB tables
        for table, permission in tables:
            if permission == _READ:
                table.grant_read_data(_lambda)
            elif permission == _WRITE:
                table.grant_write_data(_lambda)
            else:
                table.grant_full_access(_lambda)

        return _lambda

    def add(
        self,
        http_method: str,
        resource_name: str,
        tables: list[TablePermissionTuple],
        create_queue: bool = False,
    ) -> LambdaQueueTuple:
        # create resource under self.api.root if it doesn't already exist
        _resource: apigw.Resource = None
        if resource_name in self.apigw_resources:
            _resource = self.apigw_resources[resource_name]
        if not _resource:
            _resource = self.api.root.add_resource(resource_name)
            self.apigw_resources[resource_name] = _resource

        # create lambda
        _id = f"{resource_name}_{http_method}"
        _queue = sqs.Queue(self, resource_name) if create_queue else None
        _lambda: lambda_.Function = self.create_lambda(_id, tables, _queue)
        if _queue:
            _queue.grant_send_messages(_lambda)

        # add method to resource as proxy to _lambda
        _resource.add_method(http_method, apigw.LambdaIntegration(_lambda))

        return LambdaQueueTuple(_lambda, _queue)

    def import_dynamodb_table(self, name: str) -> dynamodb.Table:
        return dynamodb.Table.from_table_name(self, name, f"{self.prefix}_{name}")