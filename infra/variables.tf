variable "aws_region" {
  description = "AWS region to deploy into. The workshop account is constrained to us-east-1 for most actions."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Short name used for resource names and tags."
  type        = string
  default     = "video-upload-portal"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,30}$", var.project_name))
    error_message = "project_name must be lowercase alphanumeric with hyphens, start with a letter, and be 3-31 characters."
  }
}

variable "environment" {
  description = "Environment label for tags."
  type        = string
  default     = "hackathon"
}

variable "categories" {
  description = "Folder categories provisioned in the bucket. Each id becomes the S3 prefix used by the portal."
  type        = list(string)
  default = [
    "raw-videos",
    "video-clips",
    "frames",
    "detections",
  ]
}

variable "allowed_http_cidr" {
  description = "CIDR allowed to reach the public ALB on port 80."
  type        = string
  default     = "0.0.0.0/0"
}

variable "vpc_cidr" {
  description = "CIDR block for the portal VPC."
  type        = string
  default     = "10.42.0.0/16"
}

variable "public_subnet_cidrs" {
  description = "Two public subnet CIDRs for the ALB and Fargate tasks."
  type        = list(string)
  default     = ["10.42.1.0/24", "10.42.2.0/24"]
}

variable "container_image" {
  description = "Fully qualified container image for the FastAPI app. If empty, Terraform uses the managed ECR repository with app_image_tag."
  type        = string
  default     = ""
}

variable "app_image_tag" {
  description = "Image tag to use with the managed ECR repository when container_image is empty."
  type        = string
  default     = "latest"
}

variable "desired_count" {
  description = "Number of Fargate tasks to run."
  type        = number
  default     = 1
}

variable "task_cpu" {
  description = "Fargate task CPU units."
  type        = number
  default     = 512
}

variable "task_memory" {
  description = "Fargate task memory in MiB."
  type        = number
  default     = 1024
}

variable "container_port" {
  description = "Port exposed by the FastAPI container."
  type        = number
  default     = 8000
}

variable "force_destroy_bucket" {
  description = "Allow terraform destroy to delete the video bucket even when it contains objects."
  type        = bool
  default     = true
}
