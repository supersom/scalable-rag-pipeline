# Infrastructure Architecture

This document describes the AWS infrastructure defined under `infra/`, including
the Terraform-managed resources, Karpenter node provisioning, access-control
boundaries, runtime traffic flows, and known implementation concerns.

## Annotated infrastructure diagram

```text
AWS account / us-east-1
│
├── Terraform remote state
│   └── S3: rag-platform-terraform-state-prod-7649
│       Stores Terraform's record of deployed resources. Native S3 lockfiles
│       prevent concurrent state updates.
│
└── VPC: 10.0.0.0/16
    Provides an isolated network spanning three Availability Zones.
    │
    ├── Public subnets
    │   Host resources that need a route to or from the internet.
    │   │
    │   ├── Kubernetes load balancers
    │   │   Route external traffic to applications running in EKS.
    │   │
    │   └── Single NAT Gateway
    │       Gives private workloads outbound internet access without making
    │       those workloads directly reachable from the internet.
    │
    ├── Private subnets
    │   Host compute workloads that should not receive direct internet traffic.
    │   │
    │   └── Amazon EKS 1.30
    │       Managed Kubernetes control plane for the RAG platform.
    │       │
    │       ├── Managed system node group
    │       │   Runs essential cluster services such as DNS, Karpenter,
    │       │   ingress controllers, and other critical add-ons.
    │       │   ├── 2-5 x m6i.large instances
    │       │   └── CriticalAddonsOnly taint
    │       │       Prevents normal application pods from consuming
    │       │       system-node capacity.
    │       │
    │       ├── EBS CSI add-on
    │       │   Allows Kubernetes PersistentVolumeClaims to create and attach
    │       │   AWS EBS disks.
    │       │   └── EBS CSI IAM role
    │       │       Authorizes the add-on to manage EBS volumes.
    │       │
    │       ├── Karpenter controller
    │       │   Watches for unschedulable pods and dynamically creates or
    │       │   removes EC2 worker nodes.
    │       │   ├── Controller IAM role
    │       │   │   Authorizes Karpenter to inspect and manage EC2 capacity.
    │       │   └── Node IAM role
    │       │       Lets new EC2 workers join EKS, pull images, use SSM, and
    │       │       read cached model weights.
    │       │
    │       ├── EC2NodeClass: default
    │       │   Defines shared configuration for Karpenter-created nodes:
    │       │   Amazon Linux 2023, IAM role, subnets, security groups, and a
    │       │   100-GiB encrypted gp3 root disk.
    │       │
    │       ├── CPU NodePool
    │       │   Creates general-purpose workers for CPU-based application,
    │       │   ingestion, and processing workloads.
    │       │   ├── m6i, c6i, or r6i instance families
    │       │   ├── Spot or On-Demand capacity
    │       │   ├── Aggregate limit of 1,000 CPUs
    │       │   └── Consolidation after 30 seconds when empty or underutilized
    │       │
    │       ├── GPU NodePool
    │       │   Creates GPU workers for embedding or LLM inference.
    │       │   ├── g5.2xlarge On-Demand instances
    │       │   ├── nvidia.com/gpu taint
    │       │   │   Restricts expensive GPU nodes to GPU-aware workloads.
    │       │   ├── Limits: 1,000 CPUs, 4,000 GiB memory, and 100 GPUs
    │       │   └── Consolidation after 30 seconds when empty or underutilized
    │       │
    │       └── Ray worker IRSA role
    │           Gives only the default/ray-worker Kubernetes service account
    │           permission to list, read, and write document objects in S3.
    │
    ├── Database subnets
    │   Isolate managed databases from public and application subnets.
    │   │
    │   ├── PostgreSQL database
    │   │   Stores application data such as chat history. Exactly one option
    │   │   is created based on the db_tier Terraform variable:
    │   │   │
    │   │   ├── RDS PostgreSQL 15 -- default
    │   │   │   Low-cost development and test option.
    │   │   │   ├── db.t3.micro
    │   │   │   ├── 20 GiB storage
    │   │   │   └── Single-AZ with no standby
    │   │   │
    │   │   └── Aurora PostgreSQL 15
    │   │       Higher-availability production option.
    │   │       ├── Serverless v2, scaling from 2-64 ACUs
    │   │       └── Two instances for failover
    │   │
    │   ├── PostgreSQL security group
    │   │   Allows internal VPC connections to PostgreSQL port 5432.
    │   │
    │   ├── ElastiCache Redis
    │   │   Provides semantic caching and rate-limiting storage.
    │   │   ├── cache.t4g.medium primary
    │   │   ├── One replica for failover
    │   │   └── Encryption at rest and in transit
    │   │
    │   └── Redis security group
    │       Allows internal VPC connections to Redis port 6379.
    │
    ├── VPC endpoints
    │   Keep selected AWS service traffic on the AWS private network and
    │   reduce NAT Gateway usage.
    │   │
    │   ├── S3 gateway endpoint
    │   │   Provides private access to document and model-cache buckets.
    │   │
    │   ├── ECR API endpoint
    │   │   Handles private container-registry authentication and metadata.
    │   │
    │   ├── ECR Docker endpoint
    │   │   Handles container-image layer downloads.
    │   │
    │   ├── STS endpoint
    │   │   Handles IRSA exchanges from Kubernetes identities to IAM roles.
    │   │
    │   ├── Secrets Manager endpoint
    │   │   Provides private access if workloads use AWS Secrets Manager.
    │   │
    │   └── Endpoint security group
    │       Permits HTTPS access to interface endpoints from inside the VPC.
    │
    └── Neo4j support
        │
        ├── Neo4j security group
        │   Allows internal access to:
        │   ├── 7687 -- Bolt database protocol
        │   └── 7474 -- Neo4j HTTP interface
        │
        └── Neo4j Helm deployment
            Runs Neo4j inside Kubernetes. Terraform only prepares a network
            security group; the actual deployment is managed elsewhere.
```

### External storage

```text
S3 document bucket
Stores source documents uploaded for ingestion.
├── Versioning
│   Retains previous versions of overwritten documents.
├── Transfer Acceleration
│   Improves long-distance uploads through AWS edge locations.
├── CORS policy
│   Allows the configured frontend domain to upload directly to S3.
├── Intelligent-Tiering lifecycle
│   Transitions objects after 30 days for automatic cost optimization.
└── Ray worker access policy
    Allows ingestion workers to list, download, and upload documents.

S3 model-cache bucket
Caches Hugging Face model snapshots for GPU-node cold starts.
├── No versioning
│   Assumes snapshots are immutable and replaceable.
├── S3 VPC endpoint access
│   Avoids NAT charges when nodes download model files.
└── Karpenter node-role access
    Allows dynamically created workers to read cached model weights.
```

## Runtime flow

```text
User
  -> Public load balancer
    -> EKS application pods
      ├── S3 documents and model cache
      ├── PostgreSQL chat and application data
      ├── Redis cache and rate limits
      └── Neo4j graph data

Pending Kubernetes pod
  -> Karpenter detects insufficient capacity
    -> Karpenter selects the CPU or GPU NodePool
      -> EC2 worker is created in a private subnet
        -> Worker joins EKS
          -> Pod is scheduled
            -> Empty or underutilized worker is later consolidated
```

## Architecture writeup

Terraform establishes a three-AZ VPC with separate public, private, and database
subnet tiers. EKS workers run in private subnets, while public subnets are
reserved for load balancers and the NAT Gateway. Traffic to S3, ECR, STS, and
Secrets Manager uses VPC endpoints to remain on the AWS network and reduce NAT
traffic.

EKS has a small, always-on managed node group intended for system workloads.
Application capacity is supplied dynamically by Karpenter. CPU workloads can
use several Intel instance families and either Spot or On-Demand capacity. GPU
workloads are restricted to On-Demand `g5.2xlarge` instances and require the
corresponding GPU taint toleration.

Terraform creates the Karpenter IAM infrastructure, but it does not install the
Karpenter controller or apply its `EC2NodeClass` and `NodePool` resources.
`scripts/bootstrap_cluster.sh` performs those steps after Terraform completes.
The same separation applies to Neo4j: Terraform only creates an access security
group, while the database itself is deployed through Helm.

The relational database is configurable. A fresh deployment defaults to an
inexpensive, single-AZ RDS PostgreSQL instance. Setting `db_tier = "aurora"`
replaces that with a two-instance Aurora Serverless v2 cluster. Changing tiers
on an existing environment destroys the old database, so required data must be
migrated first.

Redis is always provisioned as an encrypted, two-node replication group. It is
intended for semantic caching and rate limiting rather than durable primary
storage.

The document bucket supports versioned source-document storage, accelerated
uploads, browser CORS, and automatic storage-tier optimization. The separate
model bucket caches replaceable model snapshots so new GPU nodes can retrieve
weights through the S3 VPC endpoint rather than through the NAT Gateway.

## Why there are multiple security groups and IAM roles

Security groups and IAM roles protect different layers:

- Security groups control network reachability: which ports accept traffic and
  where that traffic may originate.
- IAM roles control AWS API authorization: which AWS operations a pod,
  controller, add-on, or EC2 node may perform.

The design mostly creates one boundary for each service or workload identity.
This increases the visible resource count, but limits the impact of a
compromised component. For example, a compromised Ray worker should not inherit
Karpenter's permission to launch EC2 instances.

### Security groups

| Security group | Responsibility |
| --- | --- |
| VPC endpoint security group | Allows HTTPS from the VPC to private ECR, STS, and Secrets Manager endpoints. |
| RDS PostgreSQL security group | Allows internal PostgreSQL traffic on port 5432. |
| Redis security group | Allows internal Redis traffic on port 6379. |
| Neo4j security group | Allows internal Bolt and HTTP traffic on ports 7687 and 7474. |
| EKS-managed security groups | Control communication between the EKS control plane and worker nodes. |

### IAM roles

| IAM role | Responsibility |
| --- | --- |
| Karpenter controller role | Lets Karpenter inspect capacity and create, tag, and terminate EC2 workers. |
| Karpenter node role | Lets dynamically created workers join EKS, pull ECR images, use SSM, and read model weights. |
| EBS CSI controller role | Lets the EBS CSI add-on create, attach, detach, and manage EBS volumes. |
| Ray worker IRSA role | Lets the `default/ray-worker` service account list, read, and write the document bucket. |
| EKS managed-node roles | Let the baseline system nodes operate as EKS workers. These are created by the EKS module. |

The appropriate simplification strategy is to remove unused boundaries and
tighten broad permissions. Combining all services into one security group or
IAM role would reduce resource count while increasing the potential blast
radius.

## Provisioning and ownership boundaries

| Concern | Managed by |
| --- | --- |
| VPC, subnets, endpoints, NAT, and security groups | Terraform |
| EKS cluster and baseline managed node group | Terraform |
| EBS CSI add-on and its IAM role | Terraform |
| Karpenter controller and node IAM roles | Terraform |
| Karpenter Helm release | `scripts/bootstrap_cluster.sh` |
| Karpenter EC2NodeClass and NodePools | YAML under `infra/karpenter/`, applied by the bootstrap script |
| RDS or Aurora, Redis, and S3 buckets | Terraform |
| Neo4j network security group | Terraform |
| Neo4j application deployment | Helm outside this folder |

## Terraform outputs

Terraform exposes:

- EKS cluster name and API endpoint.
- The active RDS or Aurora writer endpoint.
- The Redis primary endpoint.
- Document and model-cache bucket names.
- The Karpenter controller role ARN.
- The Karpenter node role name.

These outputs allow deployment scripts and application configuration to consume
the generated resource identifiers without duplicating them.

## Validation status

At the time of review:

- `terraform validate` passed.
- `terraform fmt -check -diff` reported formatting-only differences in
  `eks.tf`, `iam.tf`, `karpenter.tf`, `main.tf`, `redis.tf`, and `vpc.tf`.

## Review findings

1. **The state-locking README is stale.** The existing README says DynamoDB
   provides state locking, but `main.tf` uses native S3 lockfiles and provisions
   no DynamoDB table.

2. **The Karpenter examples in the README are stale.** The GPU pool uses
   `g5.2xlarge`, while the CPU pool permits `m6i`, `c6i`, and `r6i` families.

3. **Karpenter manifests contain hardcoded environment values.** The cluster
   name, Karpenter node-role name, and `Environment: prod` tag are not derived
   from Terraform variables. Changing `cluster_name` or `environment` will not
   automatically update the YAML.

4. **The EKS API endpoint is public.** Authentication is protected by AWS IAM,
   but no source CIDR restriction is configured for the public endpoint.

5. **Database security groups are broad.** PostgreSQL, Redis, and Neo4j accept
   their service ports from the entire VPC rather than only from specific EKS
   worker or workload security groups.

6. **Model-bucket access is attached to every Karpenter node.** This avoids
   requiring a pod service-account annotation during model initialization, but
   grants model-read access to CPU and GPU nodes alike.

7. **Some Karpenter controller permissions use `resources = ["*"]`.** Several
   EC2 and instance-profile operations require broad scoping, but the policy
   should be compared against the current upstream recommended policy and
   constrained with tags and conditions where possible.

8. **The Neo4j security group appears unattached in this folder.** Terraform
   creates it, but the reviewed infrastructure does not demonstrate how the
   Helm-deployed Neo4j workload uses it.

9. **Database deletion is intentionally destructive.** Both PostgreSQL options
   skip final snapshots. Switching tiers or destroying the stack can
   permanently remove chat history.

10. **Production-named S3 buckets allow forced deletion.** Both document and
    model buckets use `force_destroy = true`, so Terraform can remove non-empty
    buckets during teardown.

11. **A single NAT Gateway is a cost-availability tradeoff.** It reduces cost
    but creates a cross-AZ dependency and a single outbound-internet failure
    point for traffic not handled by VPC endpoints.

12. **Secrets Manager is prepared but not used for the database password.** A
    private endpoint exists, but no secret resource is defined. The database
    password is passed into Terraform and remains sensitive data in Terraform
    state.

13. **Explicit S3 hardening is incomplete.** Public-access block resources,
    explicit bucket-encryption resources, and restrictive bucket policies are
    not defined. AWS service defaults provide some protection, but production
    controls should be explicit and reviewable in code.

14. **The document CORS origin is a placeholder.** It is currently restricted
    to `https://your-rag-domain.com` and must be replaced with the deployed
    frontend origin before browser uploads work.

## Source map

- `terraform/main.tf`: providers, remote state, and default tags.
- `terraform/vpc.tf`: VPC, subnets, NAT, endpoint security group, and VPC
  endpoints.
- `terraform/eks.tf`: EKS cluster and baseline managed node group.
- `terraform/ebs-csi.tf`: EBS CSI IAM role and EKS add-on.
- `terraform/karpenter.tf`: Karpenter controller and worker-node IAM resources.
- `karpenter/provisioner-cpu.yaml`: shared EC2NodeClass and CPU NodePool.
- `karpenter/provisioner-gpu.yaml`: GPU NodePool.
- `terraform/rds.tf`: selectable RDS or Aurora PostgreSQL database.
- `terraform/redis.tf`: ElastiCache Redis replication group.
- `terraform/neo4j.tf`: Neo4j network security group.
- `terraform/s3.tf`: document and model-cache buckets.
- `terraform/iam.tf`: Ray worker document-bucket IRSA policy and role.
- `terraform/outputs.tf`: values exported for deployment and configuration.
