output "project_id" {
  value       = var.project_id
  description = "GCP Project ID."
}

output "region" {
  value       = var.region
  description = "GCP Region."
}

output "artifact_registry_repository" {
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${var.artifact_repo_name}"
  description = "Docker repository URI in Artifact Registry."
}

output "bigquery_dataset" {
  value       = google_bigquery_dataset.family_finance.dataset_id
  description = "BigQuery dataset for family finance."
}

output "cloud_run_service_account" {
  value       = google_service_account.monarch_run.email
  description = "Service account used by Cloud Run."
}

output "cloud_build_deploy_command" {
  value       = "gcloud builds submit --config=cloudbuild.yaml --project=${var.project_id}"
  description = "Command to build and deploy application revisions via Cloud Build."
}
