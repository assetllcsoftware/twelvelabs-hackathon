output "alb_url" {
  description = "Public URL for the FastAPI upload portal."
  value       = "http://${aws_lb.app.dns_name}"
}

output "bucket_name" {
  description = "Private S3 bucket that stores raw videos."
  value       = aws_s3_bucket.videos.id
}

output "categories" {
  description = "Folder categories provisioned in the bucket."
  value       = local.category_ids
}

output "prefixes" {
  description = "S3 prefixes (one per category) used by the portal."
  value       = local.prefixes
}

output "ecr_repository_url" {
  description = "ECR repository URL for the FastAPI container image."
  value       = aws_ecr_repository.app.repository_url
}

output "shared_token_secret_arn" {
  description = "Secrets Manager secret ARN containing the shared portal token."
  value       = aws_secretsmanager_secret.portal_token.arn
}

output "shared_token_lookup_command" {
  description = "Command to retrieve the generated shared portal token."
  value       = "aws secretsmanager get-secret-value --secret-id ${aws_secretsmanager_secret.portal_token.arn} --query SecretString --output text --region ${var.aws_region}"
}
