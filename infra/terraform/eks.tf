# infra/terraform/eks.tf

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name    = var.cluster_name
  cluster_version = "1.35" # Standard support until ~mid-2027; 1.30 hit extended-support pricing

  # Networking: Connect to the VPC we just created
  vpc_id                         = module.vpc.vpc_id
  subnet_ids                     = module.vpc.private_subnets
  cluster_endpoint_public_access = true # Allow developer access from internet (secured by IAM)

  # OIDC Provider is REQUIRED for Service Accounts (IRSA)
  # This allows a specific Pod to assume an AWS IAM Role
  enable_irsa = true

  # NODE GROUPS (The "Always On" Baseline)
  eks_managed_node_groups = {
    # System Node Group: Runs CoreDNS, Karpenter, Ingress Controller
    system = {
      name           = "system-nodes"
      instance_types = ["m6i.large"] # Modern Intel generation
      min_size       = 2
      max_size       = 5
      desired_size   = 2
      
      # Taints prevent App pods from scheduling here accidentally
      taints = [
        {
          key    = "CriticalAddonsOnly"
          value  = "true"
          effect = "NO_SCHEDULE"
        }
      ]
    }
  }

  # Prepare security groups
  node_security_group_tags = {
    "karpenter.sh/discovery" = var.cluster_name
  }

  # Cluster auth via EKS access entries (module v20 dropped aws-auth ConfigMap management).
  # EC2_LINUX grants the node role the system:nodes bootstrap permissions the
  # aws-auth mapping used to provide, so Karpenter-launched nodes can join.
  authentication_mode = "API"

  # Without this the applying IAM principal gets no cluster-admin access entry
  # and kubectl is locked out of the fresh cluster (v20 defaults it to false).
  enable_cluster_creator_admin_permissions = true

  access_entries = {
    karpenter_nodes = {
      principal_arn = aws_iam_role.karpenter_node.arn
      type          = "EC2_LINUX"
    }
  }
}

# Export the Cluster Endpoint
output "cluster_endpoint" {
  value = module.eks.cluster_endpoint
}