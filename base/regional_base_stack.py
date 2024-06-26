from tagging import add_tags
from aws_cdk import (
    RemovalPolicy,
    Stack,
    aws_ec2 as ec2,
    aws_s3 as s3,
)
from constructs import Construct


class RegionalBaseStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        prefix: str,
        region: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # S3 buckets
        self.models_bucket = s3.Bucket(
            self,
            f"{prefix}_models",
            bucket_name=f"{prefix}-models-{region}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        add_tags(self.models_bucket, {"bucket": "models"})

        self.logs_bucket = s3.Bucket(
            self,
            f"{prefix}_logs",
            bucket_name=f"{prefix}-logs-{region}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        add_tags(self.logs_bucket, {"bucket": "logs"})

        # VPC
        self.vpc = ec2.Vpc(
            self,
            "VPC",
            vpc_name="neurodeploy-vpc",
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                ),
                ec2.SubnetConfiguration(
                    name="private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                ),
            ],
            nat_gateways=0,
            gateway_endpoints={
                "s3": ec2.GatewayVpcEndpointOptions(
                    service=ec2.GatewayVpcEndpointAwsService.S3
                ),
                "dynamodb": ec2.GatewayVpcEndpointOptions(
                    service=ec2.GatewayVpcEndpointAwsService.DYNAMODB
                ),
            },
        )
        self.vpc.add_interface_endpoint(
            f"{prefix}-lambda-interface-endpoint",
            service=ec2.InterfaceVpcEndpointAwsService.LAMBDA_,
            subnets=ec2.SubnetSelection(subnets=self.vpc.private_subnets),
        )
