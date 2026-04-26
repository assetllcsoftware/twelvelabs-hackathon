data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_iam_policy_document" "ecs_tasks_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

locals {
  common_tags = {
    Project     = var.project_name
    Environment = var.environment
    ManagedBy   = "terraform"
  }

  az_names       = slice(data.aws_availability_zones.available.names, 0, 2)
  category_ids   = [for c in var.categories : trim(c, "/")]
  prefixes       = [for c in local.category_ids : "${c}/"]
  container_name = "video-upload-portal"
  app_image      = var.container_image != "" ? var.container_image : "${aws_ecr_repository.app.repository_url}:${var.app_image_tag}"
}

resource "random_id" "suffix" {
  byte_length = 4
}

resource "random_password" "portal_token" {
  length  = 24
  special = false
}

locals {
  # Use the explicit var when set; otherwise fall back to the random one so
  # a fresh `terraform apply` without overrides still produces a working
  # secret.
  portal_token_value = var.portal_token != "" ? var.portal_token : random_password.portal_token.result
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-vpc"
  })
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-igw"
  })
}

resource "aws_subnet" "public" {
  count = 2

  vpc_id                  = aws_vpc.this.id
  cidr_block              = var.public_subnet_cidrs[count.index]
  availability_zone       = local.az_names[count.index]
  map_public_ip_on_launch = true

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-public-${count.index + 1}"
  })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-public-rt"
  })
}

resource "aws_route_table_association" "public" {
  count = length(aws_subnet.public)

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_s3_bucket" "videos" {
  bucket        = "${var.project_name}-${random_id.suffix.hex}"
  force_destroy = var.force_destroy_bucket

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-videos"
  })
}

resource "aws_s3_bucket_public_access_block" "videos" {
  bucket = aws_s3_bucket.videos.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "videos" {
  bucket = aws_s3_bucket.videos.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "videos" {
  bucket = aws_s3_bucket.videos.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_cors_configuration" "videos" {
  bucket = aws_s3_bucket.videos.id

  cors_rule {
    allowed_headers = ["*"]
    allowed_methods = ["GET", "HEAD", "PUT", "POST", "DELETE"]
    allowed_origins = ["*"]
    expose_headers  = ["ETag"]
    max_age_seconds = 3600
  }
}

resource "aws_s3_object" "category_prefix" {
  for_each = toset(local.prefixes)

  bucket       = aws_s3_bucket.videos.id
  key          = each.value
  content      = ""
  content_type = "application/x-directory"
}

resource "aws_secretsmanager_secret" "portal_token" {
  name                    = "${var.project_name}-${random_id.suffix.hex}-shared-token"
  recovery_window_in_days = 0

  tags = local.common_tags
}

resource "aws_secretsmanager_secret_version" "portal_token" {
  secret_id     = aws_secretsmanager_secret.portal_token.id
  secret_string = local.portal_token_value
}

resource "aws_ecr_repository" "app" {
  name         = var.project_name
  force_delete = true

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = local.common_tags
}

resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${var.project_name}"
  retention_in_days = 7

  tags = local.common_tags
}

resource "aws_ecs_cluster" "this" {
  name = var.project_name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = local.common_tags
}

resource "aws_iam_role" "task_execution" {
  name               = "${var.project_name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume_role.json

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "task_execution_managed" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "task_execution_secret" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.portal_token.arn]
  }
}

resource "aws_iam_role_policy" "task_execution_secret" {
  name   = "${var.project_name}-read-secret"
  role   = aws_iam_role.task_execution.id
  policy = data.aws_iam_policy_document.task_execution_secret.json
}

resource "aws_iam_role" "task" {
  name               = "${var.project_name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume_role.json

  tags = local.common_tags
}

data "aws_iam_policy_document" "task_s3" {
  statement {
    sid       = "ListPortalPrefixes"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.videos.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = concat(local.prefixes, [for p in local.prefixes : "${p}*"])
    }
  }

  statement {
    sid = "CrudPortalPrefixes"
    actions = [
      "s3:AbortMultipartUpload",
      "s3:DeleteObject",
      "s3:GetObject",
      "s3:ListMultipartUploadParts",
      "s3:PutObject"
    ]
    resources = [for p in local.prefixes : "${aws_s3_bucket.videos.arn}/${p}*"]
  }

  # The search path needs to presign URLs for frame thumbs (PUT by the
  # frame-embed worker) and for Bedrock async clip output (PUT by Bedrock).
  # Both live under embeddings/. Read-only is enough — the portal never
  # writes to those prefixes itself.
  statement {
    sid       = "ReadEmbeddingsArtifacts"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.videos.arn}/embeddings/*"]
  }
}

resource "aws_iam_role_policy" "task_s3" {
  name   = "${var.project_name}-s3-portal"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_s3.json
}

# --- Bedrock InvokeModel for /api/search/* (Phase D.2) ---
#
# We use the cross-region inference profile id (`us.twelvelabs....`) at
# call time, so the task role needs permission on:
#   * the inference-profile ARN itself, and
#   * the underlying foundation-model ARN in every region the profile may
#     route to. The `us.` profile spans us-east-1 / us-east-2 / us-west-2;
#     we use a `*` region wildcard to keep the policy short and to avoid
#     having to chase AWS's region list when they expand the profile.

data "aws_caller_identity" "current" {}

locals {
  marengo_model_id     = "twelvelabs.marengo-embed-3-0-v1:0"
  marengo_inference_id = "us.twelvelabs.marengo-embed-3-0-v1:0"
}

data "aws_iam_policy_document" "task_bedrock_invoke" {
  statement {
    sid     = "InvokeMarengoForSearch"
    actions = ["bedrock:InvokeModel"]
    resources = [
      "arn:aws:bedrock:*::foundation-model/${local.marengo_model_id}",
      "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/${local.marengo_inference_id}",
    ]
  }
}

resource "aws_iam_role_policy" "task_bedrock_invoke" {
  name   = "${var.project_name}-bedrock-invoke"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_bedrock_invoke.json
}

resource "aws_security_group" "alb" {
  name        = "${var.project_name}-alb"
  description = "Allow public HTTP to the video upload portal"
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "HTTP"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = [var.allowed_http_cidr]
  }

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-alb"
  })
}

resource "aws_security_group" "app" {
  name        = "${var.project_name}-app"
  description = "Allow ALB traffic to the FastAPI task"
  vpc_id      = aws_vpc.this.id

  ingress {
    description     = "FastAPI from ALB"
    from_port       = var.container_port
    to_port         = var.container_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-app"
  })
}

resource "aws_lb" "app" {
  name               = substr(replace(var.project_name, "_", "-"), 0, 32)
  load_balancer_type = "application"
  internal           = false
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  tags = local.common_tags
}

resource "aws_lb_target_group" "app" {
  name        = substr("${replace(var.project_name, "_", "-")}-tg", 0, 32)
  port        = var.container_port
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.this.id

  health_check {
    enabled             = true
    healthy_threshold   = 2
    interval            = 20
    matcher             = "200"
    path                = "/health"
    protocol            = "HTTP"
    timeout             = 5
    unhealthy_threshold = 3
  }

  tags = local.common_tags
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.app.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

resource "aws_ecs_task_definition" "app" {
  family                   = var.project_name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([
    {
      name      = local.container_name
      image     = local.app_image
      essential = true

      portMappings = [
        {
          containerPort = var.container_port
          hostPort      = var.container_port
          protocol      = "tcp"
        }
      ]

      environment = [
        {
          name  = "AWS_REGION"
          value = var.aws_region
        },
        {
          name  = "S3_BUCKET"
          value = aws_s3_bucket.videos.id
        },
        {
          name  = "PORTAL_CATEGORIES"
          value = join(",", local.category_ids)
        },
        {
          name  = "RUN_MIGRATIONS"
          value = "1"
        },
        {
          # Same JSON the yolo-detect worker consumes. Reused here so the
          # search API can surface per-model UI hints (mask_only) without a
          # separate config.
          name  = "YOLO_MODELS"
          value = var.yolo_detect_models_json
        }
      ]

      secrets = [
        {
          name      = "UPLOAD_PORTAL_TOKEN"
          valueFrom = aws_secretsmanager_secret.portal_token.arn
        },
        {
          # Pulls the `url` JSON key out of the DB secret. ECS does the
          # parsing using the task execution role; the container only sees
          # a plain libpq URL via env.
          name      = "DATABASE_URL"
          valueFrom = "${aws_secretsmanager_secret.db.arn}:url::"
        }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.app.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "app"
        }
      }
    }
  ])

  tags = local.common_tags
}

resource "aws_ecs_service" "app" {
  name            = var.project_name
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    assign_public_ip = true
    security_groups  = [aws_security_group.app.id]
    subnets          = aws_subnet.public[*].id
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = local.container_name
    container_port   = var.container_port
  }

  depends_on = [aws_lb_listener.http]

  tags = local.common_tags
}
