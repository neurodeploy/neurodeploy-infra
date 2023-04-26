from typing import NamedTuple, Tuple, List, Dict
import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigateway as apigw,
    aws_certificatemanager as acm,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_lambda_event_sources as event_sources,
    aws_route53 as route53,
    aws_route53_targets as target,
    aws_s3 as s3,
    aws_secretsmanager as sm,
    aws_sqs as sqs,
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
_IAM_FULL_PERMISSION_POLICY = "IAMFullAccess"

_JWT_SECRET_NAME = "jwt_secret"
_USER_API = "user-api"


class MainStack(Stack):
    def import_dynamodb_table(self, name: str) -> dynamodb.ITable:
        return dynamodb.Table.from_table_name(self, name, f"{self.prefix}_{name}")

    def import_databases(self):
        self.users: dynamodb.Table = self.import_dynamodb_table("Users")
        self.creds: dynamodb.Table = self.import_dynamodb_table("Creds")
        self.models: dynamodb.Table = self.import_dynamodb_table("Models")
        self.usages: dynamodb.Table = self.import_dynamodb_table("Usages")

    def import_secrets(self):
        self.jwt_secret = sm.Secret.from_secret_name_v2(
            self,
            "jwt_secret",
            secret_name=_JWT_SECRET_NAME,
        )

    def import_lambda_layers(self):
        jwt_layer_arn = {
            "prod": {
                "us-west-1": "arn:aws:lambda:us-west-1:410585721938:layer:pyjwt:1",
                "us-east-2": "arn:aws:lambda:us-east-2:410585721938:layer:pyjwt:1",
            },
            "dev": {"us-east-1": "arn:aws:lambda:us-east-1:460216766486:layer:pyjwt:1"},
        }

        self.py_jwt_layer = lambda_.LayerVersion.from_layer_version_arn(
            self,
            "py_jwt_layer",
            layer_version_arn=jwt_layer_arn[self.env_][self.region],
        )

    def import_hosted_zone(self) -> route53.IHostedZone:
        zone = route53.HostedZone.from_lookup(
            self,
            "HostedZone",
            domain_name=self.domain_name,
        )
        return zone

    def create_lambda(
        self,
        id: str,
        tables: List[Tuple[dynamodb.Table, Permission]] = None,
        buckets: List[Tuple[s3.Bucket, Permission]] = None,
        layers: List[lambda_.LayerVersion] = None,
        queue: sqs.Queue = None,
    ) -> lambda_.Function:
        # environment variables for the lambda function
        env = {table.table_name: table.table_arn for (table, _) in tables or []}
        env["region_name"] = self.region_name
        env["prefix"] = self.prefix
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
        api: apigw.RestApi,
        http_method: str,
        resource_name: str,
        proxy: bool = False,
        filename_overwrite: str = None,
        tables: List[Tuple[dynamodb.Table, Permission]] = None,
        buckets: List[Tuple[s3.Bucket, Permission]] = None,
        secrets: List[Tuple[str, sm.Secret]] = None,
        layers: List[lambda_.LayerVersion] = None,
        create_queue: bool = False,
    ) -> LambdaQueueTuple:
        # create resource under self.api.root if it doesn't already exist
        _resource: apigw.Resource = None
        if (resource_name, proxy) in self.apigw_resources:
            _resource = self.apigw_resources[(resource_name, proxy)]
        # didn't exist
        if not _resource:
            if not proxy:
                _resource = api.root.add_resource(resource_name)
            else:
                if (resource_name, False) in self.apigw_resources:
                    _resource = self.apigw_resources[(resource_name, False)]
                else:
                    _resource = api.root.add_resource(resource_name)
                    self.apigw_resources[(resource_name, False)] = _resource
                _resource = _resource.add_proxy(any_method=False)
            self.apigw_resources[(resource_name, proxy)] = _resource

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
            _id if not filename_overwrite else filename_overwrite,
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

    def create_cert_for_domain(self) -> acm.Certificate:
        return acm.Certificate(
            self,
            "Certificate",
            domain_name=f"*.{self.domain_name}",
            validation=acm.CertificateValidation.from_dns(self.hosted_zone),
        )

    def create_api_gateway_and_lambdas(
        self,
    ) -> Tuple[apigw.RestApi, Dict[str, LambdaQueueTuple]]:
        self.apigw_resources: Dict[str, Dict] = {}

        api = apigw.RestApi(
            self,
            id=f"{self.prefix}_api",
            endpoint_types=[apigw.EndpointType.REGIONAL],
        )
        domain_name = apigw.DomainName(
            self,
            f"{self.domain_name}_domain_name",
            mapping=api,
            certificate=self.main_cert,
            domain_name=f"{_USER_API}.{self.domain_name}",
        )

        POST_signup = self.add(
            api,
            "POST",
            "sign-up",
            filename_overwrite="signup_POST",
            tables=[(self.users, _READ_WRITE), (self.creds, _READ_WRITE)],
            secrets=[("jwt_secret", self.jwt_secret)],
            layers=[self.py_jwt_layer],
            create_queue=True,
        )
        POST_signup.lambda_function.add_environment("domain_name", self.domain_name)
        POST_signup.lambda_function.add_environment(
            "hosted_zone_id", self.hosted_zone.hosted_zone_id
        )
        POST_signup.lambda_function.role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name(_ACM_FULL_PERMISSION_POLICY)
        )
        OPTIONS_signup = self.add(
            api,
            "OPTIONS",
            "sign-up",
            filename_overwrite="signup_OPTIONS",
        )

        POST_signin = self.add(
            api,
            "POST",
            "sign-in",
            filename_overwrite="signin_POST",
            tables=[(self.users, _READ), (self.creds, _READ_WRITE)],
            secrets=[("jwt_secret", self.jwt_secret)],
            layers=[self.py_jwt_layer],
        )
        OPTIONS_signin = self.add(
            api,
            "OPTIONS",
            "sign-in",
            filename_overwrite="signin_OPTIONS",
        )

        # credentials
        # Note: need read-write permission for GET due to use of PartiQL
        OPTIONS_credentials = self.add(
            api,
            "OPTIONS",
            "credentials",
            filename_overwrite="credentials_OPTIONS",
        )
        OPTIONS_proxy_credentials = self.add(
            api,
            "OPTIONS",
            "credentials",
            filename_overwrite="credentials_proxy_OPTIONS",
            proxy=True,
        )
        GET_creds = self.add(
            api,
            "GET",
            "credentials",
            filename_overwrite="credentials_GET",
            tables=[(self.users, _READ), (self.creds, _READ_WRITE)],
            secrets=[("jwt_secret", self.jwt_secret)],
            layers=[self.py_jwt_layer],
        )

        POST_access_creds = self.add(
            api,
            "POST",
            "credentials",
            filename_overwrite="credentials_POST",
            tables=[(self.users, _READ), (self.creds, _READ_WRITE)],
            secrets=[("jwt_secret", self.jwt_secret)],
            layers=[self.py_jwt_layer],
        )

        DELETE_access_creds = self.add(
            api,
            "DELETE",
            "credentials",
            filename_overwrite="credentials_DELETE",
            proxy=True,
            tables=[(self.users, _READ), (self.creds, _READ_WRITE)],
            secrets=[("jwt_secret", self.jwt_secret)],
            layers=[self.py_jwt_layer],
        )

        # ml-models
        OPTIONS_ml_models = self.add(
            api,
            "OPTIONS",
            "ml-models",
            filename_overwrite="ml_models_OPTIONS",
        )
        OPTIONS_ml_models_proxy = self.add(
            api,
            "OPTIONS",
            "ml-models",
            filename_overwrite="ml_models_proxy_OPTIONS",
            proxy=True,
        )
        DELETE_ml_models = self.add(
            api,
            "DELETE",
            "ml-models",
            filename_overwrite="ml_models_DELETE",
            proxy=True,
            tables=[(self.users, _READ), (self.creds, _READ)],
            secrets=[("jwt_secret", self.jwt_secret)],
            layers=[self.py_jwt_layer],
        )
        self.models_bucket.grant_read_write(DELETE_ml_models.lambda_function)
        for policy in [_IAM_FULL_PERMISSION_POLICY, _APIGW_FULL_PERMISSION_POLICY]:
            DELETE_ml_models.lambda_function.role.add_managed_policy(
                iam.ManagedPolicy.from_aws_managed_policy_name(policy)
            )

        PUT_ml_models = self.add(
            api,
            "PUT",
            "ml-models",
            filename_overwrite="ml_models_PUT",
            proxy=True,
            tables=[
                (self.users, _READ),
                (self.creds, _READ),
                (self.models, _READ_WRITE),
            ],
            secrets=[("jwt_secret", self.jwt_secret)],
            layers=[self.py_jwt_layer],
        )
        PUT_ml_models.lambda_function.role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name(
                _APIGW_FULL_PERMISSION_POLICY
            )
        )
        self.models_bucket.grant_read_write(PUT_ml_models.lambda_function)
        self.staging_bucket.grant_read_write(PUT_ml_models.lambda_function)

        GET_ml_models = self.add(
            api,
            "GET",
            "ml-models",
            filename_overwrite="ml_models_GET",
            proxy=True,
            tables=[
                (self.users, _READ),
                (self.creds, _READ),
                (self.usages, _READ_WRITE),
            ],
            buckets=[(self.models_bucket, _READ)],
            secrets=[("jwt_secret", self.jwt_secret)],
            layers=[self.py_jwt_layer],
        )

        GET_list_of_ml_models = self.add(
            api,
            "GET",
            "ml-models",
            filename_overwrite="ml_models_list_GET",
            tables=[
                (self.users, _READ),
                (self.creds, _READ),
                (self.usages, _READ),
            ],
            buckets=[(self.models_bucket, _READ)],
            secrets=[("jwt_secret", self.jwt_secret)],
            layers=[self.py_jwt_layer],
        )

        # DNS records
        target = route53.CfnRecordSet.AliasTargetProperty(
            dns_name=domain_name.domain_name_alias_domain_name,
            hosted_zone_id=domain_name.domain_name_alias_hosted_zone_id,
            evaluate_target_health=False,
        )

        self.api_record = route53.CfnRecordSet(
            self,
            "UserApiARecord",
            name=f"{_USER_API}.{self.domain_name}",
            type="A",
            alias_target=target,
            hosted_zone_id=self.hosted_zone.hosted_zone_id,
            weight=100,
            set_identifier=f"user-{cdk.Aws.STACK_NAME}",
        )

        return (
            api,
            {
                "POST_signup": POST_signup,
                "POST_signin": POST_signin,
                "GET_creds": GET_creds,
                "PUT_ml_models": PUT_ml_models,
                "DELETE_ml_models": DELETE_ml_models,
                "GET_ml_models": GET_ml_models,
                "GET_list_of_ml_models": GET_list_of_ml_models,
            },
        )

    def create_new_user_lambda(self) -> lambda_.Function:
        new_user_lambda = lambda_.Function(
            self,
            "new_user_lambda",
            function_name=f"{self.prefix}_new_user",
            runtime=lambda_.Runtime.PYTHON_3_9,
            code=lambda_.Code.from_asset("src"),
            handler="new_user.handler",
            timeout=Duration.seconds(300),
            environment={
                "hosted_zone_id": self.hosted_zone.hosted_zone_id,
                "region_name": self.region_name,
                "queue": self.POST_signup.queue.queue_url,
            },
            layers=[],
            reserved_concurrent_executions=2,
        )
        self.POST_signup.queue.grant_consume_messages(new_user_lambda)
        self.POST_signup.queue.grant_send_messages(new_user_lambda)
        new_user_lambda.add_event_source(
            event_sources.SqsEventSource(self.POST_signup.queue, batch_size=1)
        )
        permissions = [
            _ACM_FULL_PERMISSION_POLICY,
            _SQS_FULL_PERMISSION_POLICY,
            _ROUTE_53_FULL_PERMISSION_POLICY,
            _APIGW_FULL_PERMISSION_POLICY,
        ]
        for permission in permissions:
            new_user_lambda.role.add_managed_policy(
                iam.ManagedPolicy.from_aws_managed_policy_name(permission)
            )

        return new_user_lambda

    def create_delete_user_lambda(self) -> lambda_.Function:
        delete_queue = sqs.Queue(
            self,
            "delete_queue",
            visibility_timeout=Duration.minutes(15),
            retention_period=Duration.hours(12),
            fifo=True,
            content_based_deduplication=False,
            deduplication_scope=sqs.DeduplicationScope.MESSAGE_GROUP,
        )
        delete_user_lambda = lambda_.Function(
            self,
            "delete_user_lambda",
            function_name=f"{self.prefix}_delete_user",
            runtime=lambda_.Runtime.PYTHON_3_9,
            code=lambda_.Code.from_asset("src"),
            handler="delete_user.handler",
            timeout=Duration.seconds(300),
            environment={
                "hosted_zone_id": self.hosted_zone.hosted_zone_id,
                "region_name": self.region_name,
                "queue": delete_queue.queue_url,
            },
            layers=[],
            reserved_concurrent_executions=2,
        )
        delete_queue.grant_send_messages(delete_user_lambda)
        delete_queue.grant_consume_messages(delete_user_lambda)
        permissions = [
            _ACM_FULL_PERMISSION_POLICY,
            _SQS_FULL_PERMISSION_POLICY,
            _ROUTE_53_FULL_PERMISSION_POLICY,
            _APIGW_FULL_PERMISSION_POLICY,
        ]
        for permission in permissions:
            delete_user_lambda.role.add_managed_policy(
                iam.ManagedPolicy.from_aws_managed_policy_name(permission)
            )

        return delete_user_lambda

    def create_proxy_lambda(self) -> Tuple[lambda_.Alias, LambdaQueueTuple]:
        execution_lambda = lambda_.DockerImageFunction(
            self,
            "execution_lambda",
            function_name=f"{self.prefix}_execution",
            code=lambda_.DockerImageCode.from_ecr(
                repository=ecr.Repository.from_repository_name(
                    self, "lambda_runtime_ecr", "lambda_runtime"
                ),
                tag_or_digest=self.lambda_image_digest,
            ),
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(subnets=self.subnets.subnets),
            timeout=Duration.seconds(28),
            environment={
                "region_name": self.region_name,
                "bucket": self.models_bucket.bucket_name,
                "base_image": self.lambda_image_digest,
                "domain_name": self.domain_name,
            },
            memory_size=3008,
            security_groups=[self.sg],
        )
        execution_version = execution_lambda.current_version
        execution_alias = lambda_.Alias(
            self,
            "execution_alias",
            alias_name="prod",
            version=execution_version,
            provisioned_concurrent_executions=1,
        )
        self.models_bucket.grant_read_write(execution_alias)

        proxy_lambda = lambda_.Function(
            self,
            "proxy_lambda",
            function_name=f"{self.prefix}_proxy",
            runtime=lambda_.Runtime.PYTHON_3_9,
            code=lambda_.Code.from_asset("src"),
            handler="proxy.handler",
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(subnets=self.subnets.subnets),
            timeout=Duration.seconds(30),
            environment={
                "region_name": self.region_name,
                "lambda": execution_alias.function_arn,
                "prefix": self.prefix,
            },
            security_groups=[self.sg],
        )
        execution_alias.grant_invoke(proxy_lambda)
        self.models.grant_read_data(proxy_lambda)

        logs_queue = sqs.Queue(
            self,
            "logs_queue",
            visibility_timeout=Duration.minutes(15),
            retention_period=Duration.hours(12),
            fifo=True,
            content_based_deduplication=False,
            deduplication_scope=sqs.DeduplicationScope.MESSAGE_GROUP,
        )

        # S3 permission
        self.models_bucket.grant_read(proxy_lambda)
        self.logs_bucket.grant_read_write(proxy_lambda)

        # DynamoDB permission
        self.usages.grant_full_access(proxy_lambda)
        proxy_lambda.add_environment(self.usages.table_name, self.usages.table_arn)

        # SQS queue permission
        logs_queue.grant_send_messages(proxy_lambda)

        # Lambda Rest API
        proxy_api = apigw.LambdaRestApi(
            self,
            "proxy_api",
            handler=proxy_lambda,
            proxy=True,
            domain_name=apigw.DomainNameOptions(
                certificate=self.main_cert,
                domain_name=f"api.{self.domain_name}",
                endpoint_type=apigw.EndpointType.REGIONAL,
            ),
            deploy=True,
            endpoint_types=[apigw.EndpointType.REGIONAL],
        )
        route53.ARecord(
            self,
            "proxy-api-a-record",
            zone=self.hosted_zone,
            target=route53.RecordTarget.from_alias(target.ApiGateway(api=proxy_api)),
            record_name=f"api.{self.domain_name}",
        )

        return execution_alias, LambdaQueueTuple(proxy_lambda, logs_queue)

    def create_security_group(self) -> ec2.SecurityGroup:
        sg = ec2.SecurityGroup(
            self,
            "proxy_lambda_sg",
            vpc=self.vpc,
            security_group_name="proxy_lambda_sg",
            allow_all_outbound=True,
        )
        sg.connections.allow_internally(
            port_range=ec2.Port.all_traffic(),
            description="Allow all traffic from the same security group",
        )
        return sg

    def create_s3_staging_trigger(self):
        # create lambda and have it triggered by
        # s3 bucket: self.staging_bucket
        # moving the file over to s3 bucket self.models_bucket
        # and log stuff in the dynamodb table self.models
        self.staging_trigger = lambda_.Function(
            self,
            "staging_trigger",
            function_name=f"{self.prefix}-staging-trigger",
            runtime=lambda_.Runtime.PYTHON_3_9,
            code=lambda_.Code.from_asset("src"),
            handler="s3_staging_trigger.handler",
            environment={
                "prefix": self.prefix,
            },
            timeout=Duration.seconds(29),
        )
        self.staging_bucket.grant_read_write(self.staging_trigger)
        self.models_bucket.grant_read_write(self.staging_trigger)
        self.models.grant_full_access(self.staging_trigger)

        self.staging_trigger.add_event_source(self.staging_s3_trigger)

    def create_staging_bucket(self):
        self.staging_bucket = s3.Bucket(
            self,
            f"{self.prefix}_staging",
            bucket_name=f"{self.prefix}-staging-{self.region_name}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        self.staging_s3_trigger = event_sources.S3EventSource(
            self.staging_bucket, events=[s3.EventType.OBJECT_CREATED]
        )

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        prefix: str,
        domain_name: str,
        region_name: str,
        account_number: str,
        buckets: Dict[str, s3.Bucket],
        vpc: ec2.Vpc,
        lambda_image: str,
        env_: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.env_ = env_
        self.account_number = account_number
        self.prefix = prefix
        self.region_name = region_name
        self.domain_name = domain_name

        self.models_bucket = buckets["models_bucket"]
        self.logs_bucket = buckets["models_bucket"]
        self.create_staging_bucket()

        self.vpc = vpc
        self.subnets = self.vpc.select_subnets(
            subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
        )

        self.lambda_image_digest = lambda_image

        # Imports
        self.import_secrets()
        self.import_lambda_layers()
        self.import_databases()

        # DNS
        self.hosted_zone = self.import_hosted_zone()
        self.main_cert = self.create_cert_for_domain()

        # security group for proxy lambda & execution lambda
        self.sg = self.create_security_group()

        # proxy lambda + logs queue
        self.execution_alias, self.proxy = self.create_proxy_lambda()

        # API Gateway and lambda-integrated routes
        self.api, rest = self.create_api_gateway_and_lambdas()
        self.POST_signup = rest["POST_signup"]
        self.POST_signin = rest["POST_signin"]
        self.GET_creds = rest["GET_creds"]
        self.PUT_ml_models = rest["PUT_ml_models"]
        self.GET_ml_models = rest["GET_ml_models"]
        self.GET_list_of_ml_models = rest["GET_list_of_ml_models"]
        self.DELETE_ml_models = rest["DELETE_ml_models"]

        # Additional lambdas
        self.new_user_lambda = self.create_new_user_lambda()
        self.delete_user_lambda = self.create_delete_user_lambda()

        # Trigger lambda when new file is uploaded to staging bucket
        self.create_s3_staging_trigger()
