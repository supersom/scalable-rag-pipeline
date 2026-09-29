# Shared EKS Cluster for Staging Evidence Validation

**Status:** Proposed; awaiting user review  
**Scope:** PR 5a staging prerequisites and cluster ownership

## Problem

PR 5a's staging Terraform root (`infra/terraform-staging`) currently reads the EKS cluster,
VPC, OIDC provider, and database subnet group as existing infrastructure. In the configured AWS
account and region, the VPC and database subnet group are present, but `rag-platform-cluster` is
not. Terraform therefore cannot finish planning the staging stack, and Kubernetes resources have
no cluster or namespace in which to run.

The repository already defines the shared cluster in `infra/terraform/eks.tf`. The cluster is
created by Terraform; `scripts/bootstrap_cluster.sh` runs afterward to configure kubeconfig and
install optional in-cluster components. The bootstrap stages that install Karpenter and KubeRay
must not run as part of staging evidence setup.

## Goals

- Allow the canonical infrastructure Terraform state to create or reconcile the shared
  `rag-platform-cluster` using its existing EKS configuration.
- Keep a single Terraform state responsible for the EKS cluster and avoid split ownership between
  `infra/terraform` and `infra/terraform-staging`.
- Let staging evidence resources and workloads use that same EKS cluster, AWS account, and region,
  isolated in the `staging-evidence` Kubernetes namespace.
- Keep Karpenter and KubeRay controllers and their provisioner resources out of this staging
  workflow.
- Preserve explicit review of infrastructure plans; staging deployment must not silently apply
  shared or production infrastructure.

## Non-goals

- Creating a second EKS cluster or a separate VPC for staging.
- Moving cluster ownership into the staging Terraform state.
- Automatically applying Terraform from `deploy_staging_evidence.sh`.
- Installing Karpenter, KubeRay, GPU node pools, or Ray model-serving workloads for PR 5a.
- Changing the existing baseline EKS configuration or resizing its managed node group.

## Ownership and configuration

`infra/terraform` remains the canonical owner of the shared cluster. It already declares the
cluster name, Kubernetes version, VPC/private subnets, endpoint access, IRSA/OIDC, authentication
mode, creator access, and baseline managed node group. The baseline group (two `m6i.large`
instances with the existing taint and scaling limits) remains part of the cluster configuration;
it is distinct from the optional Karpenter-managed capacity and is needed to host ordinary cluster
workloads.

`infra/terraform-staging` remains a consumer. Its EKS, VPC, OIDC, and subnet lookups are retained;
it does not declare an EKS resource or try to fall back from a failed data lookup to creating one.
This keeps the two state files from managing the same cluster. The normal owner-state behavior
handles the missing-cluster case: when the cluster is absent from the canonical state, its plan
shows the resources to create; when the cluster is already managed there, Terraform plans only
necessary drift corrections. If a cluster exists in AWS but is not tracked in the canonical state,
it must be imported or reconciled deliberately before applying—staging must not adopt it
implicitly. When the cluster is absent from both AWS and the canonical state, that state's plan
shows it as a create; absence from state alone is not proof that the AWS resource is absent.

The root `infra/terraform` configuration contains Karpenter IAM resources, but installation of
the Karpenter controller and NodePools occurs in bootstrap stages 2–3. KubeRay installation occurs
in bootstrap stage 7. The staging workflow must not run those stages. Creating the EKS cluster and
its baseline managed node group does not install either system.

## Provisioning and staging flow

1. Initialize and plan the canonical owner state (`infra/terraform`) in the configured account
   and region. Review the complete plan, paying attention to changes beyond EKS because the root
   also owns other platform infrastructure. Do not use automatic apply or hide changes with
   `-target`.
2. Apply the reviewed canonical plan explicitly. If the EKS cluster is missing from that state,
   the plan should create it using `infra/terraform/eks.tf`; otherwise, the owner state reconciles
   it normally.
3. Update local kubeconfig for `rag-platform-cluster`. Do not run bootstrap stages 2, 3, or 7
   (Karpenter controller, Karpenter NodePools, or KubeRay operator) for this staging setup.
4. Initialize, plan, and apply the separate staging evidence state. Its lookups must now resolve
   the shared cluster, VPC, OIDC provider, and database subnet group. It creates only the staging
   database and evidence resources described by PR 5a.
5. Run the staging evidence deployment into the `staging-evidence` namespace. Existing PR 5a
   safeguards remain: synthetic fixtures only, evidence metadata enabled in staging, and content
   capture disabled.

Each Terraform apply and Kubernetes deployment remains an explicit operator action after review;
the staging deploy script must fail clearly when its shared-cluster prerequisite is unavailable.

## Failure handling

- If the canonical plan cannot read or create the cluster, stop before applying staging Terraform
  or Kubernetes manifests and report the AWS account, region, and missing prerequisite without
  exposing credentials.
- If the staging data-source lookup fails, treat that as a prerequisite failure. Do not create an
  EKS cluster in the staging state as a fallback.
- If Terraform discovers an existing cluster that is not tracked by the canonical state, stop for
  a deliberate import/ownership decision. Never let two roots manage the same cluster.
- If Karpenter/KubeRay resources appear in the proposed staging bootstrap actions, stop and correct
  the stage selection before running them.

## Validation

- `terraform fmt -check` and `terraform validate` pass for both Terraform roots.
- A plan with the cluster absent from the canonical state shows EKS and its baseline managed node
  group under `infra/terraform`, and no EKS resource under `infra/terraform-staging`.
- A plan with the cluster already present in canonical state does not propose a duplicate cluster.
- Staging Terraform's data lookups resolve after the canonical cluster is available.
- The documented staging command path does not execute bootstrap stages 2, 3, or 7.
- Existing PR 5a local tests and manifest validations remain green; no live apply is part of local
  verification.

## Risks and trade-offs

- The canonical Terraform root owns more than EKS, so its plan may include unrelated infrastructure
  changes. Requiring review of the entire plan is safer than using `-target`, but can mean the
  cluster cannot be provisioned independently without a future decomposition of that root.
- The baseline managed node group remains on while the shared cluster is used. This incurs the
  existing cluster's baseline compute cost; omitting Karpenter and KubeRay does not make EKS
  compute-free.
- Staging isolation is namespace-level for Kubernetes workloads and separate-state for its AWS
  evidence resources, not a separate cluster or account boundary.
