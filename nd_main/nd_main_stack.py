from typing import Dict, NamedTuple, Tuple
import aws_cdk as cdk
from aws_cdk import (
    Duration,
    Stack,
    aws_apigateway as apigw,
    aws_certificatemanager as acm,
    aws_sqs as sqs,
    aws_s3 as s3,
    aws_lambda as lambda_,
    aws_dynamodb as dynamodb,
    aws_route53 as route53,
    aws_route53_targets as targets,
    aws_secretsmanager as sm,
)
from constructs import Construct
from enum import Enum


class LambdaQueueTuple(NamedTuple):
    lambda_function: lambda_.Function
    queue: sqs.Queue


class Permission(Enum):
    READ = "READ"
    WRITE = "WRITE"
    READ_WRITE = "READ_WRITE"


_READ = Permission.READ
_WRITE = Permission.WRITE
_READ_WRITE = Permission.READ_WRITE


class NdMainStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        prefix: str,
        domain_name: str,
        region_name: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.prefix = prefix
        self.region_name = region_name

        # secrets
        _JWT_SECRET_NAME = "neurodeploy/mvp/jwt-secrets"
        jwt_secret = sm.Secret.from_secret_name_v2(
            self,
            "jwt_secret",
            secret_name=_JWT_SECRET_NAME,
        )

        # lambda layers
        py_jwt_layer = lambda_.LayerVersion.from_layer_version_arn(
            self,
            "py_jwt_layer",
            layer_version_arn="arn:aws:lambda:us-west-2:410585721938:layer:pyjwt:1",
        )

        # DNS
        nd_zone = route53.HostedZone.from_lookup(
            self,
            "HostedZone",
            domain_name=domain_name,
        )
        main_cert = acm.Certificate(
            self,
            "Certificate",
            domain_name=f"*.{domain_name}",
            validation=acm.CertificateValidation.from_dns(nd_zone),
        )

        # DynamoDB Table imports
        self.users: dynamodb.Table = self.import_dynamodb_table("Users")
        self.apis: dynamodb.Table = self.import_dynamodb_table("APIs")
        self.sessions: dynamodb.Table = self.import_dynamodb_table("Sessions")
        self.models: dynamodb.Table = self.import_dynamodb_table("Models")
        self.usage_logs: dynamodb.Table = self.import_dynamodb_table("UsageLogs")

        # S3 bucket imports
        self.models_bucket = s3.Bucket.from_bucket_name(
            self, "models_bucket", bucket_name=f"{prefix}-models"
        )

        # API Gateway
        self.apigw_resources: Dict[str, apigw.Resource] = {}
        self.api = apigw.RestApi(
            self,
            id=f"{prefix}_api",
            domain_name=apigw.DomainNameOptions(
                domain_name=f"api.{domain_name}", certificate=main_cert
            ),
            endpoint_types=[apigw.EndpointType.REGIONAL],
        )
        POST_signup = self.add(
            "POST",
            "signup",
            tables=[(self.users, _READ_WRITE)],
            create_queue=True,
        )
        POST_signup.lambda_function.add_environment("cert", main_cert.certificate_arn)
        POST_signin = self.add(
            "POST",
            "signin",
            tables=[
                (self.users, _READ),
                (self.sessions, _READ_WRITE),
            ],
            secrets=[("jwt_secret", jwt_secret)],
            layers=[py_jwt_layer],
        )
        GET_access_token = self.add(
            "GET",
            "access_token",
            tables=[
                (self.users, _READ_WRITE),
                (self.sessions, _READ_WRITE),
                (self.apis, _READ_WRITE),
                (self.models, _READ_WRITE),
                (self.usage_logs, _READ_WRITE),
            ],
            secrets=[("jwt_secret", jwt_secret)],
            layers=[py_jwt_layer],
        )
        POST_create_endpoint = self.add(
            "POST",
            "create_endpoint",
            tables=[
                (self.users, _READ_WRITE),
                (self.sessions, _READ_WRITE),
                (self.apis, _READ_WRITE),
                (self.models, _READ_WRITE),
                (self.usage_logs, _READ_WRITE),
            ],
            secrets=[("jwt_secret", jwt_secret)],
            layers=[py_jwt_layer],
        )
        POST_associate_ml_model = self.add(
            "POST",
            "associate_ml_model",
            tables=[
                (self.users, _READ_WRITE),
                (self.sessions, _READ_WRITE),
                (self.apis, _READ_WRITE),
                (self.models, _READ_WRITE),
                (self.usage_logs, _READ_WRITE),
            ],
            secrets=[("jwt_secret", jwt_secret)],
            layers=[py_jwt_layer],
        )
        GET_ml_model_upload = self.add(
            "GET",
            "ml_model_upload",
            tables=[
                (self.users, _READ_WRITE),
                (self.sessions, _READ_WRITE),
                (self.apis, _READ_WRITE),
                (self.models, _READ_WRITE),
                (self.usage_logs, _READ_WRITE),
            ],
            buckets=[(self.models_bucket, _READ_WRITE)],
            secrets=[("jwt_secret", jwt_secret)],
            layers=[py_jwt_layer],
        )

        # Add record to route53 pointing "api" subdomain to api gateway
        api_record = route53.ARecord(
            self,
            "ApiRecord",
            record_name=f"api.{domain_name}",
            zone=nd_zone,
            target=route53.RecordTarget.from_alias(targets.ApiGateway(self.api)),
        )

    def create_lambda(
        self,
        id: str,
        tables: list[Tuple[dynamodb.Table, Permission]] = None,
        buckets: list[Tuple[s3.Bucket, Permission]] = None,
        layers: list[lambda_.LayerVersion] = None,
        queue: sqs.Queue = None,
    ) -> lambda_.Function:
        # environment variables for the lambda function
        env = {table.table_name: table.table_arn for (table, _) in tables}
        env["region_name"] = self.region_name
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
            layers=layers or [],
        )

        # grant lambda function access to DynamoDB tables
        for table, permission in tables or []:
            if permission == _READ:
                table.grant_read_data(_lambda)
            elif permission == _WRITE:
                table.grant_write_data(_lambda)
            else:
                table.grant_full_access(_lambda)

        # grant lambda function access to S3 buckets
        for bucket, permission in buckets or []:
            if permission == _READ:
                bucket.grant_read(_lambda)
            elif permission == _WRITE:
                bucket.grant_write(_lambda)
            else:
                bucket.grant_read_write(_lambda)

        return _lambda

    def add(
        self,
        http_method: str,
        resource_name: str,
        tables: list[Tuple[dynamodb.Table, Permission]] = None,
        buckets: list[Tuple[s3.Bucket, Permission]] = None,
        secrets: list[Tuple[str, sm.Secret]] = None,
        layers: list[lambda_.LayerVersion] = None,
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
        _lambda = self.create_lambda(
            _id,
            tables=tables,
            buckets=buckets,
            layers=layers,
            queue=_queue,
        )
        if _queue:
            _queue.grant_send_messages(_lambda)

        # add method to resource as proxy to _lambda
        _resource.add_method(http_method, apigw.LambdaIntegration(_lambda))

        # grant lambda permission to read secret
        for secret_name, secret in secrets or []:
            secret.grant_read(_lambda)
            _lambda.add_environment(secret_name, secret.secret_value.unsafe_unwrap())

        return LambdaQueueTuple(_lambda, _queue)

    def import_dynamodb_table(self, name: str) -> dynamodb.ITable:
        return dynamodb.Table.from_table_name(self, name, f"{self.prefix}_{name}")