# infra/terraform/rds.tf
#
# Two mutually exclusive database options controlled by var.db_tier:
#   "aurora" — Aurora Serverless v2, ~$86/month min, multi-AZ HA (production)
#   "rds"    — db.t3.micro RDS PostgreSQL, ~$15/month, single-AZ (dev/test)
#
# Switch by setting db_tier in terraform.tfvars and running terraform apply.
# Switching tiers on a live cluster destroys the old instance and creates a new
# one — migrate data first if the chat history matters.

# ── Aurora Serverless v2 (production) ─────────────────────────────────────────

module "aurora" {
  count   = var.db_tier == "aurora" ? 1 : 0
  source  = "terraform-aws-modules/rds-aurora/aws"
  version = "8.3.0"

  name           = "${var.cluster_name}-postgres"
  engine         = "aurora-postgresql"
  engine_version = "15.17"
  instance_class = "db.serverless"

  instances = {
    one = {}
    two = {} # second instance for HA failover
  }

  serverlessv2_scaling_configuration = {
    min_capacity = 2
    max_capacity = 64
  }

  vpc_id               = module.vpc.vpc_id
  db_subnet_group_name = module.vpc.database_subnet_group_name
  security_group_rules = {
    vpc_ingress = {
      cidr_blocks = [module.vpc.vpc_cidr_block]
    }
  }

  master_username = "ragadmin"
  master_password = var.db_password
  database_name   = "rag_db"

  skip_final_snapshot = false
}

# ── RDS PostgreSQL (dev/test) ──────────────────────────────────────────────────

resource "aws_security_group" "rds_postgres" {
  count       = var.db_tier == "rds" ? 1 : 0
  name        = "${var.cluster_name}-rds-postgres"
  description = "Allow PostgreSQL from within the VPC"
  vpc_id      = module.vpc.vpc_id

  ingress {
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }
}

resource "aws_db_instance" "postgres" {
  count = var.db_tier == "rds" ? 1 : 0

  identifier     = "${var.cluster_name}-postgres"
  engine         = "postgres"
  engine_version = "15"
  instance_class = "db.t3.micro"

  allocated_storage = 20
  db_name           = "rag_db"
  username          = "ragadmin"
  password          = var.db_password

  db_subnet_group_name   = module.vpc.database_subnet_group_name
  vpc_security_group_ids = [aws_security_group.rds_postgres[0].id]

  publicly_accessible = false
  multi_az            = false
  skip_final_snapshot = true # chat history only — safe to drop on teardown

  tags = {
    Name = "${var.cluster_name}-postgres-dev"
  }
}

# ── Shared local ───────────────────────────────────────────────────────────────

locals {
  db_endpoint = var.db_tier == "aurora" ? module.aurora[0].cluster_endpoint : aws_db_instance.postgres[0].address
}
