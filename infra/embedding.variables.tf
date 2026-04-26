variable "frame_worker_image" {
  description = "Fully qualified frame-embed-worker image. Empty -> use managed ECR repo with frame_worker_image_tag."
  type        = string
  default     = ""
}

variable "frame_worker_image_tag" {
  description = "Image tag for the managed frame-worker ECR repo when frame_worker_image is empty."
  type        = string
  default     = "latest"
}

variable "frame_worker_cpu" {
  description = "Fargate vCPU units for the frame-embed worker."
  type        = number
  default     = 1024
}

variable "frame_worker_memory" {
  description = "Fargate memory (MiB) for the frame-embed worker."
  type        = number
  default     = 2048
}

variable "frame_worker_ephemeral_storage_gib" {
  description = "Ephemeral storage (GiB) attached to each worker task. Must be 21..200."
  type        = number
  default     = 30
}

variable "frame_worker_fps" {
  description = "Frames per second sampled by ffmpeg in the worker."
  type        = number
  default     = 1
}

variable "frame_worker_width" {
  description = "Width (px) the worker scales frames to before embedding."
  type        = number
  default     = 720
}

variable "frame_worker_parallel" {
  description = "Concurrent Bedrock invoke_model + S3 PUT threads inside the worker."
  type        = number
  default     = 8
}

# ---------------------------------------------------------------------------
# Phase D.5 — Pegasus video-text worker.
# ---------------------------------------------------------------------------

variable "clip_pegasus_image" {
  description = "Fully qualified clip-pegasus-worker image. Empty -> use managed ECR repo with clip_pegasus_image_tag."
  type        = string
  default     = ""
}

variable "clip_pegasus_image_tag" {
  description = "Image tag for the managed clip-pegasus ECR repo when clip_pegasus_image is empty."
  type        = string
  default     = "latest"
}

variable "clip_pegasus_cpu" {
  description = "Fargate vCPU units for the clip-pegasus worker."
  type        = number
  default     = 1024
}

variable "clip_pegasus_memory" {
  description = "Fargate memory (MiB) for the clip-pegasus worker."
  type        = number
  default     = 2048
}

variable "clip_pegasus_ephemeral_storage_gib" {
  description = "Ephemeral storage (GiB) attached to each worker task. Must be 21..200."
  type        = number
  default     = 30
}

variable "clip_pegasus_prompt_id" {
  description = "Default Pegasus preset id baked into the task definition."
  type        = string
  default     = "inspector"
}

variable "clip_pegasus_temperature" {
  description = "Default Pegasus sampling temperature."
  type        = number
  default     = 0.0
}

# ---------------------------------------------------------------------------
# Phase D.6 — YOLO instance-segmentation worker.
# ---------------------------------------------------------------------------

variable "yolo_detect_image" {
  description = "Fully qualified yolo-detect-worker image. Empty -> use managed ECR repo with yolo_detect_image_tag."
  type        = string
  default     = ""
}

variable "yolo_detect_image_tag" {
  description = "Image tag for the managed yolo-detect ECR repo when yolo_detect_image is empty."
  type        = string
  default     = "latest"
}

variable "yolo_detect_cpu" {
  description = "Fargate vCPU units for the yolo-detect worker (CPU-only torch)."
  type        = number
  default     = 2048
}

variable "yolo_detect_memory" {
  description = "Fargate memory (MiB) for the yolo-detect worker."
  type        = number
  default     = 4096
}

variable "yolo_detect_ephemeral_storage_gib" {
  description = "Ephemeral storage (GiB) attached to each YOLO worker task."
  type        = number
  default     = 30
}

variable "yolo_detect_models_prefix" {
  description = "S3 key prefix (under the videos bucket) where YOLO weights live, e.g. 'models/yolo'."
  type        = string
  default     = "models/yolo"
}

variable "yolo_detect_models_json" {
  description = <<EOT
JSON array describing the YOLO models the worker should run. Each entry:
{
  "name":      "pldm-power-line",                       # logical key in DB + UI
  "s3_key":    "models/yolo/pldm-power-line/v1/best.pt",# location of the weights
  "version":   "v1",
  "classes":   {"0": "power_line"},                     # id -> human name
  "colors":    {"0": "#ff8c00"},                        # optional, palette default if missing
  "mask_only": true                                     # optional, hides bbox/label in UI
}

The portal task definition reads the same JSON to surface ``mask_only`` to
the search API so the UI can suppress bboxes for thin segmentation classes
(power lines) while still drawing them for chunky ones (insulators, poles).
EOT
  type        = string
  default     = <<-EOT
  [
    {
      "name": "pldm-power-line",
      "s3_key": "models/yolo/pldm-power-line/v1/best.pt",
      "version": "v1",
      "classes": {"0": "power_line"},
      "colors":  {"0": "#ff8c00"},
      "mask_only": true
    },
    {
      "name": "airpelago-insulator-pole",
      "s3_key": "models/yolo/airpelago-insulator-pole/v1/best.pt",
      "version": "v1",
      "classes": {"0": "insulator", "1": "pole"},
      "colors":  {"0": "#00e0ff", "1": "#ff5cc6"}
    }
  ]
  EOT
}

variable "yolo_detect_imgsz" {
  description = "Inference imgsz passed to ultralytics."
  type        = number
  default     = 640
}

variable "yolo_detect_conf" {
  description = "Minimum confidence threshold for kept detections."
  type        = number
  default     = 0.10
}

variable "yolo_detect_iou" {
  description = "NMS IoU threshold for kept detections."
  type        = number
  default     = 0.5
}
