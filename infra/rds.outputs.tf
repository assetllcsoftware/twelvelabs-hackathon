output "database_endpoint" {
  description = "Postgres endpoint hostname (no port)."
  value       = aws_db_instance.postgres.address
}

output "database_port" {
  description = "Postgres port."
  value       = aws_db_instance.postgres.port
}

output "database_name" {
  description = "Initial Postgres database name."
  value       = var.db_name
}

output "database_username" {
  description = "Postgres master username."
  value       = var.db_username
}

output "database_secret_arn" {
  description = "Secrets Manager secret ARN containing the Postgres connection details (host, port, dbname, username, password, url)."
  value       = aws_secretsmanager_secret.db.arn
}

output "database_secret_lookup_command" {
  description = "Command to retrieve the Postgres connection JSON from Secrets Manager."
  value       = "aws secretsmanager get-secret-value --secret-id ${aws_secretsmanager_secret.db.arn} --query SecretString --output text --region ${var.aws_region}"
}
