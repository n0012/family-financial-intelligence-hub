variable "project_id" {
  type        = string
  description = "The Google Cloud project ID (e.g. your-gcp-project-id)."
}

variable "region" {
  type        = string
  description = "Google Cloud region for resources."
  default     = "us-central1"
}

variable "service_name" {
  type        = string
  description = "Name of the Cloud Run service."
  default     = "monarch-gemini-wrapper"
}

variable "artifact_repo_name" {
  type        = string
  description = "Artifact Registry Docker repository name."
  default     = "monarch-repo"
}

variable "bigquery_dataset_id" {
  type        = string
  description = "BigQuery dataset ID for financial data."
  default     = "family_finance"
}

variable "sync_schedule" {
  type        = string
  description = "Cron schedule for the daily Monarch-to-BigQuery ingestion."
  default     = "0 4 * * *" # Daily at 4:00 AM
}

variable "alert_schedule" {
  type        = string
  description = "Cron schedule for proactive advisory alerts and fixes."
  default     = "0 8 * * *" # Daily at 8:00 AM (after 4:00 AM sync)
}

variable "time_zone" {
  type        = string
  description = "Time zone for Cloud Scheduler."
  default     = "America/New_York"
}
