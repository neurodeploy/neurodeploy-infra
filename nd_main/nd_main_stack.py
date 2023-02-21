from typing import Dict, NamedTuple, Tuple
import aws_cdk as cdk
from aws_cdk import (
    Duration,
    Stack,
    aws_apigateway as apigw,
    aws_certificatemanager as acm,
    aws_iam as iam,
    aws_sqs as sqs,
    aws_s3 as s3,
    aws_lambda as lambda_,
    aws_lambda_event_sources as event_sources,
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

_ACM_FULL_PERMISSION_POLICY = "AWSCertificateManagerFullAccess"
_SQS_FULL_PERMISSION_POLICY = "AmazonSQSFullAccess"
_ROUTE_53_FULL_PERMISSION_POLICY = "AmazonRoute53FullAccess"
_APIGW_FULL_PERMISSION_POLICY = "AmazonAPIGatewayAdministrator"


class NdMainStack(Stack):
    def import_dynamodb_table(self, name: str) -> dynamodb.ITable:
        return dynamodb.Table.from_table_name(self, name, f"{self.prefix}_{name}")

    def import_databases(self):
        self.users: dynamodb.Table = self.import_dynamodb_table("Users")
        self.apis: dynamodb.Table = self.import_dynamodb_table("APIs")
        self.tokens: dynamodb.Table = self.import_dynamodb_table("Tokens")
        self.models: dynamodb.Table = self.import_dynamodb_table("Models")
        self.usage_logs: dynamodb.Table = self.import_dynamodb_table("UsageLogs")

    def import_buckets(self):
        self.models_bucket = s3.Bucket.from_bucket_name(
            self, "models_bucket", bucket_name=f"{self.prefix}-models"
        )

    def import_secrets(self):
        _JWT_SECRET_NAME = "neurodeploy/mvp/jwt-secrets"
        self.jwt_secret = sm.Secret.from_secret_name_v2(
            self,
            "jwt_secret",
            secret_name=_JWT_SECRET_NAME,
        )

    def import_lambda_layers(self):
        self.py_jwt_layer = lambda_.LayerVersion.from_layer_version_arn(
            self,
            "py_jwt_layer",
            layer_version_arn="arn:aws:lambda:us-west-2:410585721938:layer:pyjwt:1",
        )

    def import_hosted_zone(self) -> route53.IHostedZone:
        zone = route53.HostedZone.from_lookup(
            self,
            "HostedZone",
            domain_name=self.domain_name,
        )
        return zone

    def create_api_gateway_and_lambdas(self):
        self.apigw_resources: Dict[str, apigw.Resource] = {}
        self.api = apigw.RestApi(
            self,
            id=f"{self.prefix}_api",
            domain_name=apigw.DomainNameOptions(
                domain_name=f"api.{self.domain_name}", certificate=self.main_cert
            ),
            endpoint_types=[apigw.EndpointType.REGIONAL],
        )
        self.POST_signup = self.add(
            "POST",
            "signup",
            tables=[(self.users, _READ_WRITE), (self.tokens, _READ_WRITE)],
            create_queue=True,
        )
        self.POST_signup.lambda_function.add_environment(
            "cert", self.main_cert.certificate_arn
        )
        self.POST_signup.lambda_function.add_environment(
            "domain_name", self.domain_name
        )
        self.POST_signup.lambda_function.add_environment(
            "hostd_zone_id", self.hosted_zone.hosted_zone_id
        )
        self.POST_signup.lambda_function.role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name(_ACM_FULL_PERMISSION_POLICY)
        )
        self.POST_signin = self.add(
            "POST",
            "signin",
            tables=[
                (self.users, _READ),
                (self.tokens, _READ_WRITE),
            ],
            secrets=[("jwt_secret", self.jwt_secret)],
            layers=[self.py_jwt_layer],
        )
        self.GET_access_token = self.add(
            "GET",
            "access_token",
            tables=[
                (self.users, _READ_WRITE),
                (self.tokens, _READ_WRITE),
                (self.apis, _READ_WRITE),
                (self.models, _READ_WRITE),
                (self.usage_logs, _READ_WRITE),
            ],
            secrets=[("jwt_secret", self.jwt_secret)],
            layers=[self.py_jwt_layer],
        )
        self.POST_create_endpoint = self.add(
            "POST",
            "create_endpoint",
            tables=[
                (self.users, _READ_WRITE),
                (self.tokens, _READ_WRITE),
                (self.apis, _READ_WRITE),
                (self.models, _READ_WRITE),
                (self.usage_logs, _READ_WRITE),
            ],
            secrets=[("jwt_secret", self.jwt_secret)],
            layers=[self.py_jwt_layer],
        )
        self.POST_associate_ml_model = self.add(
            "POST",
            "associate_ml_model",
            tables=[
                (self.users, _READ_WRITE),
                (self.tokens, _READ_WRITE),
                (self.apis, _READ_WRITE),
                (self.models, _READ_WRITE),
                (self.usage_logs, _READ_WRITE),
            ],
            secrets=[("jwt_secret", self.jwt_secret)],
            layers=[self.py_jwt_layer],
        )
        self.GET_ml_model_upload = self.add(
            "GET",
            "ml_model_upload",
            tables=[
                (self.users, _READ_WRITE),
                (self.tokens, _READ_WRITE),
                (self.apis, _READ_WRITE),
                (self.models, _READ_WRITE),
                (self.usage_logs, _READ_WRITE),
            ],
            buckets=[(self.models_bucket, _READ_WRITE)],
            secrets=[("jwt_secret", self.jwt_secret)],
            layers=[self.py_jwt_layer],
        )

    def create_delete_user_lambda(self):
        self.delete_user_lambda = lambda_.Function(
            self,
            "delete_user_lambda",
            function_name=f"{self.prefix}_delete_user",
            runtime=lambda_.Runtime.PYTHON_3_9,
            code=lambda_.Code.from_asset("src"),
            handler="delete_user.handler",
            timeout=Duration.seconds(300),
            environment={"hosted_zone_id": self.hosted_zone.hosted_zone_id},
            layers=[],
        )
        permissions = [
            _ACM_FULL_PERMISSION_POLICY,
            _SQS_FULL_PERMISSION_POLICY,
            _ROUTE_53_FULL_PERMISSION_POLICY,
            _APIGW_FULL_PERMISSION_POLICY,
        ]
        for permission in permissions:
            self.delete_user_lambda.role.add_managed_policy(
                iam.ManagedPolicy.from_aws_managed_policy_name(permission)
            )
        self.apis.grant_read_write_data(self.delete_user_lambda)
        self.delete_user_lambda.add_environment(
            self.apis.table_name, self.apis.table_arn
        )

    def create_new_user_lambda(self):
        self.new_user_lambda = lambda_.Function(
            self,
            "new_user_lambda",
            function_name=f"{self.prefix}_new_user",
            runtime=lambda_.Runtime.PYTHON_3_9,
            code=lambda_.Code.from_asset("src"),
            handler="new_user.handler",
            timeout=Duration.seconds(300),
            environment={"hosted_zone_id": self.hosted_zone.hosted_zone_id},
            layers=[],
        )
        self.POST_signup.queue.grant_consume_messages(self.new_user_lambda)
        self.POST_signup.queue.grant_send_messages(self.new_user_lambda)
        self.new_user_lambda.add_event_source(
            event_sources.SqsEventSource(self.POST_signup.queue, batch_size=1)
        )
        permissions = [
            _ACM_FULL_PERMISSION_POLICY,
            _SQS_FULL_PERMISSION_POLICY,
            _ROUTE_53_FULL_PERMISSION_POLICY,
            _APIGW_FULL_PERMISSION_POLICY,
        ]
        for permission in permissions:
            self.new_user_lambda.role.add_managed_policy(
                iam.ManagedPolicy.from_aws_managed_policy_name(permission)
            )
        self.apis.grant_read_write_data(self.new_user_lambda)
        self.new_user_lambda.add_environment(self.apis.table_name, self.apis.table_arn)

    def create_cert_for_domain(self) -> acm.Certificate:
        return acm.Certificate(
            self,
            "Certificate",
            domain_name=f"*.{self.domain_name}",
            validation=acm.CertificateValidation.from_dns(self.hosted_zone),
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
        _queue = (
            sqs.Queue(
                self,
                resource_name,
                visibility_timeout=Duration.minutes(15),
                retention_period=Duration.hours(12),
                fifo=True,
                content_based_deduplication=False,
                deduplication_scope=sqs.DeduplicationScope.MESSAGE_GROUP,
            )
            if create_queue
            else None
        )
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
            _lambda.add_environment(secret_name, secret.secret_name)

        return LambdaQueueTuple(_lambda, _queue)

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
        self.domain_name = domain_name

        # Imports
        self.import_secrets()
        self.import_lambda_layers()
        self.import_databases()
        self.import_buckets()

        # DNS
        self.hosted_zone = self.import_hosted_zone()
        self.main_cert = self.create_cert_for_domain()

        # API Gateway and lambda-integrated routes
        self.create_api_gateway_and_lambdas()

        # Add record to route53 pointing "api" subdomain to api gateway
        api_record = route53.ARecord(
            self,
            "ApiRecord",
            record_name=f"api.{self.domain_name}",
            zone=self.hosted_zone,
            target=route53.RecordTarget.from_alias(targets.ApiGateway(self.api)),
        )

        # Additional lambdas
        self.create_new_user_lambda()
        self.create_delete_user_lambda()
