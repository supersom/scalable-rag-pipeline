# Staging evidence Terraform

The staging state owns its Postgres database, evidence queues, IAM roles, and
the 30-day lifecycle rule for the evidence bucket. It uses the shared EKS
cluster and VPC created by `infra/terraform`.

From the repository root, run:

```bash
bash scripts/apply_staging_terraform.sh plan
bash scripts/apply_staging_terraform.sh apply
terraform -chdir=infra/terraform-staging output -raw staging_db_secret_arn
bash scripts/deploy_staging_evidence.sh
```

The wrapper checks whether this account already owns the named evidence
bucket. On the first apply it chooses `existing` and uses that bucket's SSE-KMS
key, or chooses `create` and creates a bucket and key. The selected mode is
written to the staging Terraform state as `staging_evidence_bucket_mode`.
Later wrapper runs read that output; a bucket created by Terraform stays on
the `create` path even though it now exists in AWS. If an apply stops after
creating the bucket but before writing outputs, the wrapper also recognizes
the bucket resource in Terraform state.

Use the wrapper for plans and applies. A direct `terraform apply` requires an
explicit `staging_evidence_bucket_mode` variable and will not select a mode
automatically. If the bucket is retained outside this state, it must already
have an ARN-valued SSE-KMS key; the wrapper reads that key from the bucket.
The staging lifecycle rule expires current and noncurrent objects after 30
days in either mode. A bucket and key created by this state have
`prevent_destroy` protection, so a later staging teardown cannot silently
remove retained evidence storage.
