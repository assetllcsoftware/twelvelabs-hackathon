resource "random_password" "db" {
  length  = 32
  special = false
}

resource "aws_db_subnet_group" "postgres" {
  name        = "${var.project_name}-${random_id.suffix.hex}-db"
  description = "Subnet group for the ${var.project_name} Postgres instance"
  subnet_ids  = aws_subnet.public[*].id

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-db-subnets"
  })
}

resource "aws_security_group" "db" {
  name        = "${var.project_name}-db"
  description = "Allow Postgres traffic from the ${var.project_name} ECS tasks"
  vpc_id      = aws_vpc.this.id

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-db"
  })
}

resource "aws_security_group_rule" "db_ingress_from_app" {
  type                     = "ingress"
  description              = "Postgres from ECS tasks"
  from_port                = var.db_port
  to_port                  = var.db_port
  protocol                 = "tcp"
  security_group_id        = aws_security_group.db.id
  source_security_group_id = aws_security_group.app.id
}

resource "aws_security_group_rule" "db_egress_all" {
  type              = "egress"
  description       = "All outbound"
  from_port         = 0
  to_port           = 0
  protocol          = "-1"
  security_group_id = aws_security_group.db.id
  cidr_blocks       = ["0.0.0.0/0"]
}

resource "aws_db_parameter_group" "postgres" {
  count = var.db_tune_for_pgvector ? 1 : 0

  name        = "${var.project_name}-${random_id.suffix.hex}-pg16"
  family      = "postgres16"
  description = "Tuned for pgvector / HNSW index builds on ${var.project_name}"

  parameter {
    name         = "maintenance_work_mem"
    value        = "1048576"
    apply_method = "pending-reboot"
  }

  parameter {
    name         = "max_parallel_maintenance_workers"
    value        = "2"
    apply_method = "pending-reboot"
  }

  parameter {
    name         = "max_parallel_workers_per_gather"
    value        = "2"
    apply_method = "pending-reboot"
  }

  parameter {
    name         = "work_mem"
    value        = "32768"
    apply_method = "pending-reboot"
  }

  parameter {
    name         = "effective_io_concurrency"
    value        = "200"
    apply_method = "pending-reboot"
  }

  parameter {
    name         = "random_page_cost"
    value        = "1.1"
    apply_method = "pending-reboot"
  }

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-pg16-tuned"
  })
}

resource "aws_db_instance" "postgres" {
  identifier     = "${var.project_name}-${random_id.suffix.hex}"
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  parameter_group_name = var.db_tune_for_pgvector ? aws_db_parameter_group.postgres[0].name : null

  allocated_storage     = var.db_allocated_storage
  max_allocated_storage = var.db_max_allocated_storage
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = var.db_name
  username = var.db_username
  password = random_password.db.result
  port     = var.db_port

  db_subnet_group_name   = aws_db_subnet_group.postgres.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false
  multi_az               = var.db_multi_az

  backup_retention_period = var.db_backup_retention_days
  skip_final_snapshot     = var.db_skip_final_snapshot
  deletion_protection     = var.db_deletion_protection
  apply_immediately       = true

  auto_minor_version_upgrade = true
  copy_tags_to_snapshot      = true

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-postgres"
  })
}

resource "aws_secretsmanager_secret" "db" {
  name                    = "${var.project_name}-${random_id.suffix.hex}-db"
  description             = "Connection details for the ${var.project_name} Postgres instance"
  recovery_window_in_days = 0

  tags = local.common_tags
}

resource "aws_secretsmanager_secret_version" "db" {
  secret_id = aws_secretsmanager_secret.db.id
  secret_string = jsonencode({
    engine   = "postgres"
    host     = aws_db_instance.postgres.address
    port     = aws_db_instance.postgres.port
    dbname   = var.db_name
    username = var.db_username
    password = random_password.db.result
    url      = "postgresql://${var.db_username}:${random_password.db.result}@${aws_db_instance.postgres.address}:${aws_db_instance.postgres.port}/${var.db_name}"
  })
}

data "aws_iam_policy_document" "task_execution_db_secret" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.db.arn]
  }
}

resource "aws_iam_role_policy" "task_execution_db_secret" {
  name   = "${var.project_name}-read-db-secret"
  role   = aws_iam_role.task_execution.id
  policy = data.aws_iam_policy_document.task_execution_db_secret.json
}
