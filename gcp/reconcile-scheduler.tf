# Cloud Run's stable project-number URL avoids a dependency cycle with the service module.
data "google_project" "runner_reconciler" {
  project_id = var.project_id
}

locals {
  reconcile_service_name = "github-runners-manager-${local.region_shortnames[var.region]}"
  reconcile_service_url  = "https://${local.reconcile_service_name}-${data.google_project.runner_reconciler.number}.${var.region}.run.app"
}

resource "google_service_account" "runner_reconciler" {
  project      = var.project_id
  account_id   = "github-runners-scheduler"
  display_name = "Cloud Scheduler - GitHub runner reconciliation"
}

resource "google_cloud_run_v2_service_iam_member" "runner_reconciler" {
  project    = var.project_id
  location   = var.region
  name       = local.reconcile_service_name
  role       = "roles/run.invoker"
  member     = "serviceAccount:${google_service_account.runner_reconciler.email}"
  depends_on = [module.cloud_run_github_runners_manager]
}

resource "google_cloud_scheduler_job" "runner_reconciler" {
  project          = var.project_id
  region           = var.region
  name             = "github-runners-reconcile"
  description      = "Reconcile queued GitHub jobs and idle GCE runners"
  schedule         = "*/2 * * * *"
  time_zone        = "Etc/UTC"
  attempt_deadline = "300s"
  paused           = true

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${local.reconcile_service_url}/reconcile"
    oidc_token {
      service_account_email = google_service_account.runner_reconciler.email
      audience              = local.reconcile_service_url
    }
  }

  depends_on = [google_cloud_run_v2_service_iam_member.runner_reconciler]
}
