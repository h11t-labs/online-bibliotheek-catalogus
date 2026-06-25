variable "railway_token" {
  type        = string
  description = "Railway account/team API token (or set the RAILWAY_TOKEN env var). Create one in the Railway dashboard → Account/Team Settings → Tokens."
  sensitive   = true
}

variable "project_name" {
  type        = string
  description = "Railway project name."
  default     = "online-bibliotheek-catalogus"
}

variable "service_name" {
  type        = string
  description = "Railway service name (use this value for the GitHub RAILWAY_SERVICE variable)."
  default     = "web"
}

variable "github_repo" {
  type        = string
  description = "owner/repo used to build the GHCR image reference."
  default     = "h11t-labs/online-bibliotheek-catalogus"
}

variable "image_tag" {
  type        = string
  description = "GHCR image tag to deploy (track the moving minor tag, e.g. 0.1)."
  default     = "0.1"
}

variable "subdomain" {
  type        = string
  description = "Subdomain for the *.up.railway.app public domain."
  default     = "online-bibliotheek-catalogus"
}

variable "nyt_api_key" {
  type        = string
  description = "Optional NYT Books API key; enables the NYT bestseller lists. Leave empty to skip."
  default     = ""
  sensitive   = true
}

variable "sync_hours" {
  type        = string
  description = "Run `obc sync` every N hours via the in-app scheduler (0 = off)."
  default     = "24"
}

variable "lists_hours" {
  type        = string
  description = "Run `obc lists update` + `obc normalize` every N hours (0 = off)."
  default     = "168"
}
