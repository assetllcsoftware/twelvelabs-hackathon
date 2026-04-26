# Phase D.3 + D.4 + D.5 + D.6 — clip & frame embedding + Pegasus + YOLO.
#
#   raw-videos/<file>.mp4
#       │
#       ▼  S3 EventBridge "Object Created"
#   aws_cloudwatch_event_rule.video_uploaded
#       ├──▶ start_clip_embed   (Lambda, in VPC, has DB)
#       │       └──▶ bedrock.start_async_invoke (video → embeddings/videos/<uuid>/...)
#       │
#       ├──▶ start_frame_task   (Lambda, no VPC)
#       │       └──▶ ecs.run_task → frame-embed-worker (Fargate, in VPC, has DB)
#       │               ├──▶ ffmpeg → frame_NNNNN.jpg
#       │               ├──▶ bedrock.invoke_model (image embedding, parallel)
#       │               └──▶ S3 PUT thumb + INSERT embeddings (kind='frame')
#       │
#       └──▶ start_yolo_task    (Lambda, no VPC)
#               └──▶ ecs.run_task → yolo-detect-worker (Fargate, in VPC, has DB)
#                       ├──▶ wait until frame-embed worker has rows
#                       ├──▶ download YOLO weights from s3://.../models/yolo/...
#                       ├──▶ ultralytics.YOLO.predict() per frame thumbnail
#                       └──▶ INSERT frame_detections (polygons, normalized)
#
#   embeddings/videos/<uuid>/<bedrock-id>/output.json
#       │
#       ▼  S3 EventBridge "Object Created" (suffix /output.json)
#   aws_cloudwatch_event_rule.clip_output_ready
#       └──▶ finalize_clip_embed (Lambda, in VPC, has DB)
#               ├──▶ INSERT embeddings (kind='clip')
#               └──▶ ecs.run_task → clip-pegasus-worker (Fargate, in VPC, has DB)
#                       ├──▶ ffmpeg cut each clip → derived/clips/<digest>/clip_*.mp4
#                       ├──▶ bedrock.invoke_model_with_response_stream (Pegasus)
#                       └──▶ INSERT clip_descriptions

# ---------------------------------------------------------------------------
# Bucket-level notification: route every S3 event into EventBridge, then we
# filter via individual rules below. Cheaper and easier to maintain than
# per-prefix S3 → Lambda subscriptions.
# ---------------------------------------------------------------------------

resource "aws_s3_bucket_notification" "videos_eventbridge" {
  bucket      = aws_s3_bucket.videos.id
  eventbridge = true
}

# ---------------------------------------------------------------------------
# Shared knobs.
# ---------------------------------------------------------------------------

locals {
  marengo_foundation_arns = [
    "arn:aws:bedrock:*::foundation-model/${local.marengo_model_id}",
  ]
  marengo_inference_profile_arn = "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/${local.marengo_inference_id}"

  pegasus_model_id     = "twelvelabs.pegasus-1-2-v1:0"
  pegasus_inference_id = "us.twelvelabs.pegasus-1-2-v1:0"
  pegasus_foundation_arns = [
    "arn:aws:bedrock:*::foundation-model/${local.pegasus_model_id}",
  ]
  pegasus_inference_profile_arn = "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/${local.pegasus_inference_id}"

  embedding_video_prefix = "embeddings/videos/"
  embedding_frame_prefix = "embeddings/frames/"
  derived_clips_prefix   = "derived/clips"

  lambda_subnets = aws_subnet.public[*].id
}

# ---------------------------------------------------------------------------
# Network: SGs for Lambdas (VPC-attached) and the Fargate worker. Both can
# reach RDS via dedicated ingress rules on the existing aws_security_group.db
# resource (declared in infra/rds.tf).
# ---------------------------------------------------------------------------

resource "aws_security_group" "embedding_lambda" {
  name        = "${var.project_name}-embedding-lambda"
  description = "VPC-attached embedding Lambdas (start_clip_embed, finalize_clip_embed)"
  vpc_id      = aws_vpc.this.id

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-embedding-lambda"
  })
}

resource "aws_security_group" "frame_worker" {
  name        = "${var.project_name}-frame-worker"
  description = "Fargate frame-embed worker"
  vpc_id      = aws_vpc.this.id

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-frame-worker"
  })
}

resource "aws_security_group_rule" "db_ingress_from_embedding_lambda" {
  type                     = "ingress"
  description              = "Postgres from embedding Lambdas"
  from_port                = var.db_port
  to_port                  = var.db_port
  protocol                 = "tcp"
  security_group_id        = aws_security_group.db.id
  source_security_group_id = aws_security_group.embedding_lambda.id
}

resource "aws_security_group_rule" "db_ingress_from_frame_worker" {
  type                     = "ingress"
  description              = "Postgres from Fargate frame-embed worker"
  from_port                = var.db_port
  to_port                  = var.db_port
  protocol                 = "tcp"
  security_group_id        = aws_security_group.db.id
  source_security_group_id = aws_security_group.frame_worker.id
}

resource "aws_security_group" "clip_pegasus_worker" {
  name        = "${var.project_name}-clip-pegasus-worker"
  description = "Fargate clip-pegasus worker"
  vpc_id      = aws_vpc.this.id

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-clip-pegasus-worker"
  })
}

resource "aws_security_group_rule" "db_ingress_from_clip_pegasus_worker" {
  type                     = "ingress"
  description              = "Postgres from Fargate clip-pegasus worker"
  from_port                = var.db_port
  to_port                  = var.db_port
  protocol                 = "tcp"
  security_group_id        = aws_security_group.db.id
  source_security_group_id = aws_security_group.clip_pegasus_worker.id
}

resource "aws_security_group" "yolo_detect_worker" {
  name        = "${var.project_name}-yolo-detect-worker"
  description = "Fargate yolo-detect worker"
  vpc_id      = aws_vpc.this.id

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-yolo-detect-worker"
  })
}

resource "aws_security_group_rule" "db_ingress_from_yolo_detect_worker" {
  type                     = "ingress"
  description              = "Postgres from Fargate yolo-detect worker"
  from_port                = var.db_port
  to_port                  = var.db_port
  protocol                 = "tcp"
  security_group_id        = aws_security_group.db.id
  source_security_group_id = aws_security_group.yolo_detect_worker.id
}

# ---------------------------------------------------------------------------
# VPC endpoints. Lambda ENIs in a VPC cannot have public IPs, so without a
# NAT gateway they need either VPC endpoints or to skip the VPC entirely.
# We need RDS (private), so we keep the VPC and route the AWS API calls
# through interface endpoints. Bedrock + SecretsManager are interface
# endpoints (per-AZ ENIs with private DNS); S3 is a gateway endpoint that
# attaches to the existing public route table for free.
# ---------------------------------------------------------------------------

resource "aws_security_group" "vpc_endpoints" {
  name        = "${var.project_name}-vpc-endpoints"
  description = "Allow 443 from in-VPC compute to interface endpoints"
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "HTTPS from in-VPC compute"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-vpc-endpoints"
  })
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id]

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-s3"
  })
}

resource "aws_vpc_endpoint" "bedrock_runtime" {
  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${var.aws_region}.bedrock-runtime"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.public[*].id
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-bedrock-runtime"
  })
}

resource "aws_vpc_endpoint" "secretsmanager" {
  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${var.aws_region}.secretsmanager"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.public[*].id
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-secretsmanager"
  })
}

# ---------------------------------------------------------------------------
# Lambda zip artifacts. The handlers are pure Python with one tiny native
# dep each (none for start_frame_task, pg8000 for the others). We use
# archive_file so re-runs don't churn unless the source actually changed.
#
# pg8000 is pure-Python — that's why we picked it over psycopg for the
# Lambda layer. Listing it on its own under build/<name>/site-packages/ keeps
# the runtime layout simple.
# ---------------------------------------------------------------------------

locals {
  lambda_dir       = "${path.module}/../lambda"
  lambda_build_dir = "${path.module}/../.build/lambda"
}

# We rely on the operator running `make embedding/lambda-build` (or the
# inline shell snippet below) before `terraform apply` to populate
# .build/lambda/<name>/. Terraform fingerprints the directory tree.

data "archive_file" "lambda_start_clip_embed" {
  type        = "zip"
  source_dir  = "${local.lambda_build_dir}/start_clip_embed"
  output_path = "${local.lambda_build_dir}/start_clip_embed.zip"
}

data "archive_file" "lambda_finalize_clip_embed" {
  type        = "zip"
  source_dir  = "${local.lambda_build_dir}/finalize_clip_embed"
  output_path = "${local.lambda_build_dir}/finalize_clip_embed.zip"
}

data "archive_file" "lambda_start_frame_task" {
  type        = "zip"
  source_dir  = "${local.lambda_build_dir}/start_frame_task"
  output_path = "${local.lambda_build_dir}/start_frame_task.zip"
}

data "archive_file" "lambda_start_yolo_task" {
  type        = "zip"
  source_dir  = "${local.lambda_build_dir}/start_yolo_task"
  output_path = "${local.lambda_build_dir}/start_yolo_task.zip"
}

# ---------------------------------------------------------------------------
# IAM
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# Lambdas that talk to the VPC need ec2 ENI permissions. AWS provides a
# managed policy for exactly this — saves us a wall of statements.
data "aws_iam_policy" "lambda_basic_execution" {
  arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy" "lambda_vpc_execution" {
  arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

# --- start_clip_embed -------------------------------------------------------

resource "aws_iam_role" "lambda_start_clip_embed" {
  name               = "${var.project_name}-start-clip-embed"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy_attachment" "lambda_start_clip_basic" {
  role       = aws_iam_role.lambda_start_clip_embed.name
  policy_arn = data.aws_iam_policy.lambda_basic_execution.arn
}

resource "aws_iam_role_policy_attachment" "lambda_start_clip_vpc" {
  role       = aws_iam_role.lambda_start_clip_embed.name
  policy_arn = data.aws_iam_policy.lambda_vpc_execution.arn
}

data "aws_iam_policy_document" "lambda_start_clip_embed" {
  statement {
    sid = "BedrockAsync"
    actions = [
      "bedrock:StartAsyncInvoke",
      "bedrock:GetAsyncInvoke",
      "bedrock:InvokeModel",
    ]
    resources = concat(
      local.marengo_foundation_arns,
      [
        "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:async-invoke/*",
      ],
    )
  }

  statement {
    sid       = "S3ReadInputWriteOutput"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.videos.arn, "${aws_s3_bucket.videos.arn}/*"]
  }

  statement {
    sid       = "ReadDbSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.db.arn]
  }
}

resource "aws_iam_role_policy" "lambda_start_clip_embed" {
  name   = "${var.project_name}-start-clip-embed"
  role   = aws_iam_role.lambda_start_clip_embed.id
  policy = data.aws_iam_policy_document.lambda_start_clip_embed.json
}

# --- finalize_clip_embed ---------------------------------------------------

resource "aws_iam_role" "lambda_finalize_clip_embed" {
  name               = "${var.project_name}-finalize-clip-embed"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy_attachment" "lambda_finalize_clip_basic" {
  role       = aws_iam_role.lambda_finalize_clip_embed.name
  policy_arn = data.aws_iam_policy.lambda_basic_execution.arn
}

resource "aws_iam_role_policy_attachment" "lambda_finalize_clip_vpc" {
  role       = aws_iam_role.lambda_finalize_clip_embed.name
  policy_arn = data.aws_iam_policy.lambda_vpc_execution.arn
}

data "aws_iam_policy_document" "lambda_finalize_clip_embed" {
  statement {
    sid       = "S3ReadOutput"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.videos.arn, "${aws_s3_bucket.videos.arn}/*"]
  }

  statement {
    sid       = "ReadDbSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.db.arn]
  }

  statement {
    sid       = "RunPegasusTask"
    actions   = ["ecs:RunTask"]
    resources = [replace(aws_ecs_task_definition.clip_pegasus_worker.arn, "/:[0-9]+$/", ":*")]
  }

  statement {
    sid     = "PassPegasusRoles"
    actions = ["iam:PassRole"]
    resources = [
      aws_iam_role.clip_pegasus_worker_task.arn,
      aws_iam_role.clip_pegasus_worker_execution.arn,
    ]
  }
}

resource "aws_iam_role_policy" "lambda_finalize_clip_embed" {
  name   = "${var.project_name}-finalize-clip-embed"
  role   = aws_iam_role.lambda_finalize_clip_embed.id
  policy = data.aws_iam_policy_document.lambda_finalize_clip_embed.json
}

# --- start_frame_task ------------------------------------------------------

resource "aws_iam_role" "lambda_start_frame_task" {
  name               = "${var.project_name}-start-frame-task"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy_attachment" "lambda_start_frame_basic" {
  role       = aws_iam_role.lambda_start_frame_task.name
  policy_arn = data.aws_iam_policy.lambda_basic_execution.arn
}

data "aws_iam_policy_document" "lambda_start_frame_task" {
  statement {
    sid       = "RunWorkerTask"
    actions   = ["ecs:RunTask"]
    resources = [replace(aws_ecs_task_definition.frame_embed_worker.arn, "/:[0-9]+$/", ":*")]
  }

  statement {
    sid     = "PassWorkerRoles"
    actions = ["iam:PassRole"]
    resources = [
      aws_iam_role.frame_worker_task.arn,
      aws_iam_role.frame_worker_execution.arn,
    ]
  }
}

resource "aws_iam_role_policy" "lambda_start_frame_task" {
  name   = "${var.project_name}-start-frame-task"
  role   = aws_iam_role.lambda_start_frame_task.id
  policy = data.aws_iam_policy_document.lambda_start_frame_task.json
}

# ---------------------------------------------------------------------------
# Lambda functions
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "lambda_start_clip_embed" {
  name              = "/aws/lambda/${var.project_name}-start-clip-embed"
  retention_in_days = 14
  tags              = local.common_tags
}

resource "aws_cloudwatch_log_group" "lambda_finalize_clip_embed" {
  name              = "/aws/lambda/${var.project_name}-finalize-clip-embed"
  retention_in_days = 14
  tags              = local.common_tags
}

resource "aws_cloudwatch_log_group" "lambda_start_frame_task" {
  name              = "/aws/lambda/${var.project_name}-start-frame-task"
  retention_in_days = 14
  tags              = local.common_tags
}

resource "aws_lambda_function" "start_clip_embed" {
  function_name    = "${var.project_name}-start-clip-embed"
  role             = aws_iam_role.lambda_start_clip_embed.arn
  filename         = data.archive_file.lambda_start_clip_embed.output_path
  source_code_hash = data.archive_file.lambda_start_clip_embed.output_base64sha256
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  timeout          = 60
  memory_size      = 256

  environment {
    variables = {
      AWS_ACCOUNT_ID    = data.aws_caller_identity.current.account_id
      S3_BUCKET         = aws_s3_bucket.videos.id
      DB_SECRET_ARN     = aws_secretsmanager_secret.db.arn
      MARENGO_MODEL_ID  = local.marengo_model_id
      OUTPUT_PREFIX     = trimsuffix(local.embedding_video_prefix, "/")
      EMBEDDING_OPTIONS = "visual,audio,transcription"
    }
  }

  vpc_config {
    subnet_ids         = local.lambda_subnets
    security_group_ids = [aws_security_group.embedding_lambda.id]
  }

  depends_on = [
    aws_iam_role_policy_attachment.lambda_start_clip_basic,
    aws_iam_role_policy_attachment.lambda_start_clip_vpc,
    aws_iam_role_policy.lambda_start_clip_embed,
    aws_cloudwatch_log_group.lambda_start_clip_embed,
  ]

  tags = local.common_tags
}

resource "aws_lambda_function" "finalize_clip_embed" {
  function_name    = "${var.project_name}-finalize-clip-embed"
  role             = aws_iam_role.lambda_finalize_clip_embed.arn
  filename         = data.archive_file.lambda_finalize_clip_embed.output_path
  source_code_hash = data.archive_file.lambda_finalize_clip_embed.output_base64sha256
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  timeout          = 120
  memory_size      = 512

  environment {
    variables = {
      DB_SECRET_ARN           = aws_secretsmanager_secret.db.arn
      EMBEDDING_OUTPUT_PREFIX = local.embedding_video_prefix
      PEGASUS_ECS_CLUSTER     = aws_ecs_cluster.this.name
      PEGASUS_TASK_DEFINITION = aws_ecs_task_definition.clip_pegasus_worker.family
      PEGASUS_SUBNETS         = join(",", local.lambda_subnets)
      PEGASUS_SECURITY_GROUP  = aws_security_group.clip_pegasus_worker.id
      PEGASUS_CONTAINER_NAME  = local.clip_pegasus_container_name
    }
  }

  vpc_config {
    subnet_ids         = local.lambda_subnets
    security_group_ids = [aws_security_group.embedding_lambda.id]
  }

  depends_on = [
    aws_iam_role_policy_attachment.lambda_finalize_clip_basic,
    aws_iam_role_policy_attachment.lambda_finalize_clip_vpc,
    aws_iam_role_policy.lambda_finalize_clip_embed,
    aws_cloudwatch_log_group.lambda_finalize_clip_embed,
  ]

  tags = local.common_tags
}

resource "aws_lambda_function" "start_frame_task" {
  function_name    = "${var.project_name}-start-frame-task"
  role             = aws_iam_role.lambda_start_frame_task.arn
  filename         = data.archive_file.lambda_start_frame_task.output_path
  source_code_hash = data.archive_file.lambda_start_frame_task.output_base64sha256
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  timeout          = 30
  memory_size      = 256

  environment {
    variables = {
      S3_BUCKET             = aws_s3_bucket.videos.id
      ECS_CLUSTER           = aws_ecs_cluster.this.name
      ECS_TASK_DEFINITION   = aws_ecs_task_definition.frame_embed_worker.family
      ECS_SUBNETS           = join(",", local.lambda_subnets)
      ECS_SECURITY_GROUP    = aws_security_group.frame_worker.id
      WORKER_CONTAINER_NAME = local.frame_worker_container_name
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.lambda_start_frame_basic,
    aws_iam_role_policy.lambda_start_frame_task,
    aws_cloudwatch_log_group.lambda_start_frame_task,
  ]

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# EventBridge rules
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_event_rule" "video_uploaded" {
  name        = "${var.project_name}-video-uploaded"
  description = "S3 ObjectCreated under raw-videos/ — kicks clip + frame pipelines"

  event_pattern = jsonencode({
    "source"      = ["aws.s3"]
    "detail-type" = ["Object Created"]
    "detail" = {
      "bucket" = { "name" = [aws_s3_bucket.videos.id] }
      "object" = {
        "key" = [{ "prefix" = "raw-videos/" }]
      }
    }
  })

  tags = local.common_tags
}

resource "aws_cloudwatch_event_rule" "clip_output_ready" {
  name        = "${var.project_name}-clip-output-ready"
  description = "Bedrock async clip output landed — finalize_clip_embed picks it up"

  event_pattern = jsonencode({
    "source"      = ["aws.s3"]
    "detail-type" = ["Object Created"]
    "detail" = {
      "bucket" = { "name" = [aws_s3_bucket.videos.id] }
      "object" = {
        "key" = [
          {
            "wildcard" = "embeddings/videos/*/output.json"
          }
        ]
      }
    }
  })

  tags = local.common_tags
}

# Targets

resource "aws_cloudwatch_event_target" "video_uploaded_clip" {
  rule      = aws_cloudwatch_event_rule.video_uploaded.name
  target_id = "start_clip_embed"
  arn       = aws_lambda_function.start_clip_embed.arn
}

resource "aws_cloudwatch_event_target" "video_uploaded_frame" {
  rule      = aws_cloudwatch_event_rule.video_uploaded.name
  target_id = "start_frame_task"
  arn       = aws_lambda_function.start_frame_task.arn
}

resource "aws_cloudwatch_event_target" "clip_output_ready_finalize" {
  rule      = aws_cloudwatch_event_rule.clip_output_ready.name
  target_id = "finalize_clip_embed"
  arn       = aws_lambda_function.finalize_clip_embed.arn
}

resource "aws_lambda_permission" "allow_eb_video_uploaded_clip" {
  statement_id  = "AllowEventBridgeInvokeStartClipEmbed"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.start_clip_embed.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.video_uploaded.arn
}

resource "aws_lambda_permission" "allow_eb_video_uploaded_frame" {
  statement_id  = "AllowEventBridgeInvokeStartFrameTask"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.start_frame_task.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.video_uploaded.arn
}

resource "aws_lambda_permission" "allow_eb_clip_output_ready" {
  statement_id  = "AllowEventBridgeInvokeFinalizeClipEmbed"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.finalize_clip_embed.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.clip_output_ready.arn
}

# ---------------------------------------------------------------------------
# Fargate frame-embed worker
# ---------------------------------------------------------------------------

resource "aws_ecr_repository" "frame_worker" {
  name         = "${var.project_name}-frame-worker"
  force_delete = true

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = local.common_tags
}

resource "aws_cloudwatch_log_group" "frame_worker" {
  name              = "/ecs/${var.project_name}-frame-worker"
  retention_in_days = 14
  tags              = local.common_tags
}

locals {
  frame_worker_container_name = "frame-embed-worker"
  frame_worker_image          = var.frame_worker_image != "" ? var.frame_worker_image : "${aws_ecr_repository.frame_worker.repository_url}:${var.frame_worker_image_tag}"
}

resource "aws_iam_role" "frame_worker_execution" {
  name               = "${var.project_name}-frame-worker-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume_role.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy_attachment" "frame_worker_execution_managed" {
  role       = aws_iam_role.frame_worker_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# The execution role pulls the DB secret only because ECS would inject it
# into the container automatically if we used `secrets[]`. We don't; the
# worker fetches the secret itself via the task role. Keeping this minimal.

resource "aws_iam_role" "frame_worker_task" {
  name               = "${var.project_name}-frame-worker-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume_role.json
  tags               = local.common_tags
}

data "aws_iam_policy_document" "frame_worker_task" {
  statement {
    sid       = "BedrockInvokeImage"
    actions   = ["bedrock:InvokeModel"]
    resources = concat(local.marengo_foundation_arns, [local.marengo_inference_profile_arn])
  }

  statement {
    sid       = "S3ReadVideoWriteThumbs"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.videos.arn, "${aws_s3_bucket.videos.arn}/*"]
  }

  statement {
    sid       = "ReadDbSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.db.arn]
  }
}

resource "aws_iam_role_policy" "frame_worker_task" {
  name   = "${var.project_name}-frame-worker-task"
  role   = aws_iam_role.frame_worker_task.id
  policy = data.aws_iam_policy_document.frame_worker_task.json
}

resource "aws_ecs_task_definition" "frame_embed_worker" {
  family                   = "${var.project_name}-frame-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.frame_worker_cpu
  memory                   = var.frame_worker_memory
  execution_role_arn       = aws_iam_role.frame_worker_execution.arn
  task_role_arn            = aws_iam_role.frame_worker_task.arn

  ephemeral_storage {
    size_in_gib = var.frame_worker_ephemeral_storage_gib
  }

  container_definitions = jsonencode([
    {
      name      = local.frame_worker_container_name
      image     = local.frame_worker_image
      essential = true
      environment = [
        { name = "AWS_REGION", value = var.aws_region },
        { name = "S3_BUCKET", value = aws_s3_bucket.videos.id },
        { name = "DB_SECRET_ARN", value = aws_secretsmanager_secret.db.arn },
        { name = "MARENGO_INFERENCE_ID", value = local.marengo_inference_id },
        { name = "FPS", value = tostring(var.frame_worker_fps) },
        { name = "WIDTH", value = tostring(var.frame_worker_width) },
        { name = "PARALLEL", value = tostring(var.frame_worker_parallel) },
        { name = "THUMB_PREFIX", value = trimsuffix(local.embedding_frame_prefix, "/") },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.frame_worker.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "worker"
        }
      }
    }
  ])

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# Phase D.5 — Fargate clip-pegasus worker
# ---------------------------------------------------------------------------

resource "aws_ecr_repository" "clip_pegasus_worker" {
  name         = "${var.project_name}-clip-pegasus-worker"
  force_delete = true

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = local.common_tags
}

resource "aws_cloudwatch_log_group" "clip_pegasus_worker" {
  name              = "/ecs/${var.project_name}-clip-pegasus-worker"
  retention_in_days = 14
  tags              = local.common_tags
}

locals {
  clip_pegasus_container_name = "clip-pegasus-worker"
  clip_pegasus_image          = var.clip_pegasus_image != "" ? var.clip_pegasus_image : "${aws_ecr_repository.clip_pegasus_worker.repository_url}:${var.clip_pegasus_image_tag}"
}

resource "aws_iam_role" "clip_pegasus_worker_execution" {
  name               = "${var.project_name}-clip-pegasus-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume_role.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy_attachment" "clip_pegasus_worker_execution_managed" {
  role       = aws_iam_role.clip_pegasus_worker_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "clip_pegasus_worker_task" {
  name               = "${var.project_name}-clip-pegasus-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume_role.json
  tags               = local.common_tags
}

data "aws_iam_policy_document" "clip_pegasus_worker_task" {
  statement {
    sid       = "BedrockInvokePegasus"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = concat(local.pegasus_foundation_arns, [local.pegasus_inference_profile_arn])
  }

  statement {
    sid       = "S3ReadVideoWriteCuts"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.videos.arn, "${aws_s3_bucket.videos.arn}/*"]
  }

  statement {
    sid       = "ReadDbSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.db.arn]
  }

  statement {
    sid       = "DescribeSelf"
    actions   = ["sts:GetCallerIdentity"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "clip_pegasus_worker_task" {
  name   = "${var.project_name}-clip-pegasus-task"
  role   = aws_iam_role.clip_pegasus_worker_task.id
  policy = data.aws_iam_policy_document.clip_pegasus_worker_task.json
}

resource "aws_ecs_task_definition" "clip_pegasus_worker" {
  family                   = "${var.project_name}-clip-pegasus-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.clip_pegasus_cpu
  memory                   = var.clip_pegasus_memory
  execution_role_arn       = aws_iam_role.clip_pegasus_worker_execution.arn
  task_role_arn            = aws_iam_role.clip_pegasus_worker_task.arn

  ephemeral_storage {
    size_in_gib = var.clip_pegasus_ephemeral_storage_gib
  }

  container_definitions = jsonencode([
    {
      name      = local.clip_pegasus_container_name
      image     = local.clip_pegasus_image
      essential = true
      environment = [
        { name = "AWS_REGION", value = var.aws_region },
        { name = "S3_BUCKET", value = aws_s3_bucket.videos.id },
        { name = "DB_SECRET_ARN", value = aws_secretsmanager_secret.db.arn },
        { name = "PEGASUS_INFERENCE_ID", value = local.pegasus_inference_id },
        { name = "PEGASUS_PROMPT_ID", value = var.clip_pegasus_prompt_id },
        { name = "PEGASUS_TEMPERATURE", value = tostring(var.clip_pegasus_temperature) },
        { name = "DERIVED_CLIPS_PREFIX", value = local.derived_clips_prefix },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.clip_pegasus_worker.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "worker"
        }
      }
    }
  ])

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# Phase D.6 — Fargate yolo-detect worker + start_yolo_task Lambda
# ---------------------------------------------------------------------------

resource "aws_ecr_repository" "yolo_detect_worker" {
  name         = "${var.project_name}-yolo-detect-worker"
  force_delete = true

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = local.common_tags
}

resource "aws_cloudwatch_log_group" "yolo_detect_worker" {
  name              = "/ecs/${var.project_name}-yolo-detect-worker"
  retention_in_days = 14
  tags              = local.common_tags
}

locals {
  yolo_detect_container_name = "yolo-detect-worker"
  yolo_detect_image          = var.yolo_detect_image != "" ? var.yolo_detect_image : "${aws_ecr_repository.yolo_detect_worker.repository_url}:${var.yolo_detect_image_tag}"
  yolo_models_prefix         = trim(var.yolo_detect_models_prefix, "/")
}

resource "aws_iam_role" "yolo_detect_worker_execution" {
  name               = "${var.project_name}-yolo-detect-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume_role.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy_attachment" "yolo_detect_worker_execution_managed" {
  role       = aws_iam_role.yolo_detect_worker_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "yolo_detect_worker_task" {
  name               = "${var.project_name}-yolo-detect-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume_role.json
  tags               = local.common_tags
}

data "aws_iam_policy_document" "yolo_detect_worker_task" {
  statement {
    sid     = "S3ReadFramesAndModels"
    actions = ["s3:GetObject"]
    resources = [
      "${aws_s3_bucket.videos.arn}/${trimsuffix(local.embedding_frame_prefix, "/")}/*",
      "${aws_s3_bucket.videos.arn}/${local.yolo_models_prefix}/*",
    ]
  }

  statement {
    sid       = "ReadDbSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.db.arn]
  }
}

resource "aws_iam_role_policy" "yolo_detect_worker_task" {
  name   = "${var.project_name}-yolo-detect-task"
  role   = aws_iam_role.yolo_detect_worker_task.id
  policy = data.aws_iam_policy_document.yolo_detect_worker_task.json
}

resource "aws_ecs_task_definition" "yolo_detect_worker" {
  family                   = "${var.project_name}-yolo-detect-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.yolo_detect_cpu
  memory                   = var.yolo_detect_memory
  execution_role_arn       = aws_iam_role.yolo_detect_worker_execution.arn
  task_role_arn            = aws_iam_role.yolo_detect_worker_task.arn

  ephemeral_storage {
    size_in_gib = var.yolo_detect_ephemeral_storage_gib
  }

  container_definitions = jsonencode([
    {
      name      = local.yolo_detect_container_name
      image     = local.yolo_detect_image
      essential = true
      environment = [
        { name = "AWS_REGION", value = var.aws_region },
        { name = "S3_BUCKET", value = aws_s3_bucket.videos.id },
        { name = "DB_SECRET_ARN", value = aws_secretsmanager_secret.db.arn },
        { name = "YOLO_MODELS", value = var.yolo_detect_models_json },
        { name = "YOLO_IMGSZ", value = tostring(var.yolo_detect_imgsz) },
        { name = "YOLO_CONF", value = tostring(var.yolo_detect_conf) },
        { name = "YOLO_IOU", value = tostring(var.yolo_detect_iou) },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.yolo_detect_worker.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "worker"
        }
      }
    }
  ])

  tags = local.common_tags
}

# --- start_yolo_task Lambda ------------------------------------------------

resource "aws_iam_role" "lambda_start_yolo_task" {
  name               = "${var.project_name}-start-yolo-task"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy_attachment" "lambda_start_yolo_basic" {
  role       = aws_iam_role.lambda_start_yolo_task.name
  policy_arn = data.aws_iam_policy.lambda_basic_execution.arn
}

data "aws_iam_policy_document" "lambda_start_yolo_task" {
  statement {
    sid       = "RunYoloTask"
    actions   = ["ecs:RunTask"]
    resources = [replace(aws_ecs_task_definition.yolo_detect_worker.arn, "/:[0-9]+$/", ":*")]
  }

  statement {
    sid     = "PassYoloRoles"
    actions = ["iam:PassRole"]
    resources = [
      aws_iam_role.yolo_detect_worker_task.arn,
      aws_iam_role.yolo_detect_worker_execution.arn,
    ]
  }
}

resource "aws_iam_role_policy" "lambda_start_yolo_task" {
  name   = "${var.project_name}-start-yolo-task"
  role   = aws_iam_role.lambda_start_yolo_task.id
  policy = data.aws_iam_policy_document.lambda_start_yolo_task.json
}

resource "aws_cloudwatch_log_group" "lambda_start_yolo_task" {
  name              = "/aws/lambda/${var.project_name}-start-yolo-task"
  retention_in_days = 14
  tags              = local.common_tags
}

resource "aws_lambda_function" "start_yolo_task" {
  function_name    = "${var.project_name}-start-yolo-task"
  role             = aws_iam_role.lambda_start_yolo_task.arn
  filename         = data.archive_file.lambda_start_yolo_task.output_path
  source_code_hash = data.archive_file.lambda_start_yolo_task.output_base64sha256
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  timeout          = 30
  memory_size      = 256

  environment {
    variables = {
      S3_BUCKET             = aws_s3_bucket.videos.id
      ECS_CLUSTER           = aws_ecs_cluster.this.name
      ECS_TASK_DEFINITION   = aws_ecs_task_definition.yolo_detect_worker.family
      ECS_SUBNETS           = join(",", local.lambda_subnets)
      ECS_SECURITY_GROUP    = aws_security_group.yolo_detect_worker.id
      WORKER_CONTAINER_NAME = local.yolo_detect_container_name
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.lambda_start_yolo_basic,
    aws_iam_role_policy.lambda_start_yolo_task,
    aws_cloudwatch_log_group.lambda_start_yolo_task,
  ]

  tags = local.common_tags
}

resource "aws_cloudwatch_event_target" "video_uploaded_yolo" {
  rule      = aws_cloudwatch_event_rule.video_uploaded.name
  target_id = "start_yolo_task"
  arn       = aws_lambda_function.start_yolo_task.arn
}

resource "aws_lambda_permission" "allow_eb_video_uploaded_yolo" {
  statement_id  = "AllowEventBridgeInvokeStartYoloTask"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.start_yolo_task.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.video_uploaded.arn
}
