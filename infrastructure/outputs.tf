output "project_id" {
  description = "Railway project id."
  value       = railway_project.this.id
}

output "service_id" {
  description = "Railway service id."
  value       = railway_service.web.id
}

output "service_name" {
  description = "Use this for the GitHub Actions RAILWAY_SERVICE variable."
  value       = railway_service.web.name
}

output "public_url" {
  description = "Public URL of the web service."
  value       = "https://${railway_service_domain.web.domain}"
}
