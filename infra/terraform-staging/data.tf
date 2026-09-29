# infra/terraform-staging/data.tf
#
# Looks up the existing cluster/VPC/OIDC provider rather than reapplying those modules —
# staging must never be able to recreate or modify production networking or the cluster itself.

data "aws_eks_cluster" "existing" {
  name = var.cluster_name
}

data "aws_vpc" "existing" {
  filter {
    name   = "tag:Name"
    values = ["${var.cluster_name}-vpc"]
  }
}

data "aws_subnets" "database" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.existing.id]
  }

  filter {
    name   = "tag:Name"
    values = ["*-db-*"]
  }
}

data "aws_subnet" "database" {
  for_each = toset(data.aws_subnets.database.ids)
  id       = each.value
}

data "aws_db_subnet_group" "existing" {
  name = "${var.cluster_name}-vpc"
}

data "aws_iam_openid_connect_provider" "existing" {
  url = data.aws_eks_cluster.existing.identity[0].oidc[0].issuer
}
