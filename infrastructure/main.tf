terraform {
  required_version = ">= 1.5"
  required_providers {
    railway = {
      source  = "terraform-community-providers/railway"
      version = "~> 0.5"
    }
  }
}

# Token can also be supplied via the RAILWAY_TOKEN env var instead of a tfvar.
provider "railway" {
  token = var.railway_token
}

resource "railway_project" "this" {
  name        = var.project_name
  description = "Eigen doorzoekbare catalogus van de online Bibliotheek"
  private     = true
}

locals {
  environment_id = railway_project.this.default_environment.id
  image          = "ghcr.io/${var.github_repo}:${var.image_tag}"
}

# Web service, deployed from the versioned GHCR image (see CHANGELOG / release flow).
resource "railway_service" "web" {
  name         = var.service_name
  project_id   = railway_project.this.id
  source_image = local.image
}

# Persistent SQLite + raw data live on a volume mounted at /app/data.
resource "railway_volume" "data" {
  name           = "catalog-data"
  mount_path     = "/app/data"
  environment_id = local.environment_id
  service_id     = railway_service.web.id
}

# Runtime configuration (env vars). OBC_DB is also baked into the image but set
# here too so it's explicit; the *_HOURS vars drive the in-app refresh scheduler.
resource "railway_variable" "obc_db" {
  name           = "OBC_DB"
  value          = "/app/data/catalog.db"
  environment_id = local.environment_id
  service_id     = railway_service.web.id
}

resource "railway_variable" "sync_hours" {
  name           = "OBC_SYNC_HOURS"
  value          = var.sync_hours
  environment_id = local.environment_id
  service_id     = railway_service.web.id
}

resource "railway_variable" "lists_hours" {
  name           = "OBC_LISTS_HOURS"
  value          = var.lists_hours
  environment_id = local.environment_id
  service_id     = railway_service.web.id
}

# Optional: only created when a key is provided (enables the NYT bestseller lists).
resource "railway_variable" "nyt_api_key" {
  count          = var.nyt_api_key == "" ? 0 : 1
  name           = "NYT_API_KEY"
  value          = var.nyt_api_key
  environment_id = local.environment_id
  service_id     = railway_service.web.id
}

# Public *.up.railway.app domain for the web service.
resource "railway_service_domain" "web" {
  environment_id = local.environment_id
  service_id     = railway_service.web.id
  subdomain      = var.subdomain
}
