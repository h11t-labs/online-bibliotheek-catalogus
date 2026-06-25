# Infrastructure (Terraform → Railway)

Declarative Railway setup: project, web service (from the GHCR image), persistent
volume, env vars, and a public domain. Uses the community provider
[`terraform-community-providers/railway`](https://registry.terraform.io/providers/terraform-community-providers/railway/latest/docs).

## Usage

```bash
cd infrastructure
cp terraform.tfvars.example terraform.tfvars   # then fill in railway_token
terraform init
terraform plan
terraform apply
```

Instead of putting the token in `terraform.tfvars` you can export it:

```bash
export RAILWAY_TOKEN=...        # read by the provider
# or
export TF_VAR_railway_token=... # read as the Terraform variable
```

Get a token in the Railway dashboard → Account/Team Settings → Tokens.

## After apply

- `terraform output service_name` → set this as the GitHub Actions **`RAILWAY_SERVICE`**
  variable so the deploy job runs (`gh variable set RAILWAY_SERVICE --body "$(terraform output -raw service_name)"`).
- `terraform output public_url` → the live site.

## Notes

- The repo is private, so the GHCR image is private too. Either make the package
  public (no secrets are baked into the image) or add registry credentials to the
  service in the Railway dashboard so it can pull `ghcr.io/...`.
- State is local (`terraform.tfstate`) and gitignored along with `terraform.tfvars`.
  For team use, point `terraform` at a remote backend.
- Provider attribute names can shift between provider versions; if `plan` errors,
  check the provider docs for the pinned version and adjust.
