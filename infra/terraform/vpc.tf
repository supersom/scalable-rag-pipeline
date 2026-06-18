# infra/terraform/vpc.tf

# Create the VPC (Virtual Private Cloud)
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws" # Use verified community module
  version = "5.1.0"

  name = "${var.cluster_name}-vpc"
  cidr = var.vpc_cidr

  # Define Availability Zones for High Availability (Multi-AZ)
  azs = ["${var.aws_region}a", "${var.aws_region}b", "${var.aws_region}c"]

  # PUBLIC SUBNETS: For Load Balancers and NAT Gateways
  public_subnets = ["10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24"]

  # PRIVATE SUBNETS: For EKS Nodes, RDS, and Redis (Security Best Practice)
  private_subnets = ["10.0.101.0/24", "10.0.102.0/24", "10.0.103.0/24"]

  # DATABASE SUBNETS: Specific isolation for Aurora/Redis
  database_subnets = ["10.0.201.0/24", "10.0.202.0/24", "10.0.203.0/24"]

  # Enable NAT Gateway for residual internet traffic (pip installs, HuggingFace, etc.)
  # ECR and S3 traffic bypasses NAT via VPC endpoints defined below.
  enable_nat_gateway = true
  single_nat_gateway = true # One NAT suffices for dev/test; saves ~$65/month vs one-per-AZ
  
  # Enable DNS hostnames (required for EKS)
  enable_dns_hostnames = true
  enable_dns_support   = true

  # Tag subnets so Kubernetes Load Balancers know where to go
  public_subnet_tags = {
    "kubernetes.io/role/elb" = "1"
  }
  private_subnet_tags = {
    "kubernetes.io/role/internal-elb" = "1"
    "karpenter.sh/discovery"          = var.cluster_name # Used by Karpenter
  }
}

# Security group shared by all interface endpoints — allows HTTPS from within the VPC only
resource "aws_security_group" "vpc_endpoints" {
  name        = "${var.cluster_name}-vpc-endpoints"
  description = "Allow HTTPS from within the VPC to interface endpoints"
  vpc_id      = module.vpc.vpc_id

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }
}

# S3 Gateway endpoint — free; routes S3 traffic (pip caches, model weights, ECR layers)
# over the AWS backbone instead of through the NAT Gateway
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = module.vpc.vpc_id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = concat(
    module.vpc.private_route_table_ids,
    module.vpc.public_route_table_ids,
  )
}

# ECR interface endpoints — image pulls from ECR never touch the NAT Gateway.
# ecr.dkr handles the actual layer downloads; ecr.api handles auth/manifest calls.
resource "aws_vpc_endpoint" "ecr_dkr" {
  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.ecr.dkr"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = module.vpc.private_subnets
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true
}

resource "aws_vpc_endpoint" "ecr_api" {
  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.ecr.api"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = module.vpc.private_subnets
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true
}

# STS endpoint — IRSA token exchanges (service account → IAM role) stay off-NAT
resource "aws_vpc_endpoint" "sts" {
  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.sts"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = module.vpc.private_subnets
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true
}

# Secrets Manager endpoint — Aurora password fetches during bootstrap stay off-NAT
resource "aws_vpc_endpoint" "secretsmanager" {
  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.secretsmanager"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = module.vpc.private_subnets
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true
}