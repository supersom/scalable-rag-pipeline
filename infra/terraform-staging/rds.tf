# infra/terraform-staging/rds.tf
#
# db.t3.micro, no HA — staging only needs to prove PostgreSQL transaction semantics
# (advisory-lock migrations, atomic outbox commit, lease-token claims), not Aurora's
# autoscaling or multi-AZ properties. Matches infra/terraform's own db_tier = "rds" default.

resource "aws_security_group" "staging_evidence_postgres" {
  name        = "${var.cluster_name}-staging-evidence-postgres"
  description = "Allow PostgreSQL from within the VPC"
  vpc_id      = data.aws_vpc.existing.id

  ingress {
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }
}

resource "aws_db_instance" "staging_evidence_postgres" {
  identifier     = "${var.cluster_name}-staging-evidence-postgres"
  engine         = "postgres"
  engine_version = "15"
  instance_class = "db.t3.micro"

  allocated_storage           = 20
  db_name                     = "evidence_staging"
  username                    = "ragadmin"
  manage_master_user_password = true

  db_subnet_group_name   = data.aws_db_subnet_group.existing.name
  vpc_security_group_ids = [aws_security_group.staging_evidence_postgres.id]

  publicly_accessible = false
  multi_az            = false
  skip_final_snapshot = true # disposable staging data — safe to drop on teardown

  tags = {
    Name = "${var.cluster_name}-staging-evidence-postgres"
  }
}
