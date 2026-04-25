variable "db_name" {
  description = "Initial Postgres database name."
  type        = string
  default     = "portal"

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9_]{0,62}$", var.db_name))
    error_message = "db_name must start with a letter and contain only letters, digits, or underscores (max 63 chars)."
  }
}

variable "db_username" {
  description = "Master username for the Postgres instance."
  type        = string
  default     = "portal_admin"

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9_]{0,62}$", var.db_username))
    error_message = "db_username must start with a letter and contain only letters, digits, or underscores."
  }
}

variable "db_engine_version" {
  description = "Postgres engine version. Use a major version (e.g. \"16\") to let RDS pick the latest minor."
  type        = string
  default     = "16.13"
}

variable "db_instance_class" {
  description = "RDS instance class. db.t4g.large gives 8 GiB RAM which fits modest pgvector/HNSW workloads."
  type        = string
  default     = "db.t4g.large"
}

variable "db_tune_for_pgvector" {
  description = "Attach a custom parameter group tuned for pgvector / HNSW index builds."
  type        = bool
  default     = true
}

variable "db_allocated_storage" {
  description = "Allocated storage in GiB."
  type        = number
  default     = 20
}

variable "db_max_allocated_storage" {
  description = "Upper bound for storage autoscaling in GiB. Set equal to db_allocated_storage to disable autoscaling."
  type        = number
  default     = 50
}

variable "db_multi_az" {
  description = "Whether to deploy a Multi-AZ standby. Off by default for hackathon cost."
  type        = bool
  default     = false
}

variable "db_backup_retention_days" {
  description = "Number of days to retain automated backups. Set to 0 to disable."
  type        = number
  default     = 1
}

variable "db_skip_final_snapshot" {
  description = "Skip the final snapshot on destroy. Convenient for hackathon teardown."
  type        = bool
  default     = true
}

variable "db_deletion_protection" {
  description = "Enable RDS deletion protection."
  type        = bool
  default     = false
}

variable "db_port" {
  description = "Port the Postgres instance listens on."
  type        = number
  default     = 5432
}
