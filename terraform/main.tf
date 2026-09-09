terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}

# 1. Enable Required GCP APIs
locals {
  services = [
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "bigquery.googleapis.com",
    "geminidataanalytics.googleapis.com",
    "cloudscheduler.googleapis.com",
    "aiplatform.googleapis.com",
    "iam.googleapis.com",
  ]
}

resource "google_project_service" "enabled_apis" {
  for_each                   = toset(local.services)
  project                    = var.project_id
  service                    = each.key
  disable_dependent_services = false
  disable_on_destroy         = false
}

# 2. Service Account for Cloud Run
resource "google_service_account" "monarch_run" {
  account_id   = "monarch-gemini-run"
  display_name = "Monarch Gemini Cloud Run Service Account"
  depends_on   = [google_project_service.enabled_apis]
}

# 3. Artifact Registry Repository for Docker Images
resource "google_artifact_registry_repository" "monarch_repo" {
  location      = var.region
  repository_id = var.artifact_repo_name
  description   = "Docker repository for Monarch Money Gemini & BigQuery Hub"
  format        = "DOCKER"
  depends_on    = [google_project_service.enabled_apis]
}

# 4. BigQuery Dataset
resource "google_bigquery_dataset" "family_finance" {
  dataset_id                  = var.bigquery_dataset_id
  friendly_name               = "Family Financial Intelligence Hub"
  description                 = "Stores normalized Monarch transactions, accounts, and financial optimization views."
  location                    = var.region
  default_table_expiration_ms = null

  access {
    role          = "OWNER"
    user_by_email = google_service_account.monarch_run.email
  }

  access {
    role          = "OWNER"
    special_group = "projectOwners"
  }

  depends_on = [google_project_service.enabled_apis]
}

# 5. Secret Manager: Placeholders & Generated Keys
locals {
  secrets = [
    "monarch-email",
    "monarch-password",
    "monarch-mfa-secret",
    "gemini-wrapper-key",
    "alert-webhook-url",
  ]
}

resource "google_secret_manager_secret" "secrets" {
  for_each  = toset(local.secrets)
  secret_id = each.key
  replication {
    auto {}
  }
  depends_on = [google_project_service.enabled_apis]
}

# Default placeholder versions so Cloud Run deployment succeeds before credentials are added
resource "google_secret_manager_secret_version" "alert_webhook_default" {
  secret      = google_secret_manager_secret.secrets["alert-webhook-url"].id
  secret_data = "NONE"
}

resource "google_secret_manager_secret_version" "monarch_email_placeholder" {
  secret      = google_secret_manager_secret.secrets["monarch-email"].id
  secret_data = "placeholder@example.com"
}

resource "google_secret_manager_secret_version" "monarch_password_placeholder" {
  secret      = google_secret_manager_secret.secrets["monarch-password"].id
  secret_data = "placeholder"
}

resource "google_secret_manager_secret_version" "monarch_mfa_secret_placeholder" {
  secret      = google_secret_manager_secret.secrets["monarch-mfa-secret"].id
  secret_data = "NONE"
}

# Generate a high-entropy random API key for the wrapper
resource "random_password" "wrapper_api_key" {
  length  = 48
  special = false
}

resource "google_secret_manager_secret_version" "wrapper_api_key_version" {
  secret      = google_secret_manager_secret.secrets["gemini-wrapper-key"].id
  secret_data = random_password.wrapper_api_key.result
}

# 6. IAM Policy Bindings for Cloud Run Service Account
resource "google_project_iam_member" "run_secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.monarch_run.email}"
}

resource "google_project_iam_member" "run_bq_editor" {
  project = var.project_id
  role    = "roles/bigquery.dataEditor"
  member  = "serviceAccount:${google_service_account.monarch_run.email}"
}

resource "google_project_iam_member" "run_bq_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.monarch_run.email}"
}

# 7. Cloud Build Permissions (So Cloud Build can deploy updates to Cloud Run & BigQuery)
data "google_project" "current" {
  project_id = var.project_id
}

resource "google_project_iam_member" "cloudbuild_run_admin" {
  project = var.project_id
  role    = "roles/run.admin"
  member  = "serviceAccount:${data.google_project.current.number}@cloudbuild.gserviceaccount.com"
  depends_on = [google_project_service.enabled_apis]
}

resource "google_project_iam_member" "cloudbuild_sa_user" {
  project = var.project_id
  role    = "roles/iam.serviceAccountUser"
  member  = "serviceAccount:${data.google_project.current.number}@cloudbuild.gserviceaccount.com"
  depends_on = [google_project_service.enabled_apis]
}

resource "google_project_iam_member" "cloudbuild_bq_admin" {
  project = var.project_id
  role    = "roles/bigquery.admin"
  member  = "serviceAccount:${data.google_project.current.number}@cloudbuild.gserviceaccount.com"
  depends_on = [google_project_service.enabled_apis]
}

resource "google_project_iam_member" "cloudbuild_secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${data.google_project.current.number}@cloudbuild.gserviceaccount.com"
  depends_on = [google_project_service.enabled_apis]
}

# 8. Cloud Scheduler Service Account
resource "google_service_account" "scheduler_sa" {
  account_id   = "monarch-scheduler-sa"
  display_name = "Cloud Scheduler SA for Daily Monarch BigQuery Sync"
  depends_on   = [google_project_service.enabled_apis]
}
