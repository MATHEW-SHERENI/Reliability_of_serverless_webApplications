# Results storage for the experiment runs, kept in its own Terraform stack so
# that `make destroy` (the testbed) never deletes or blocks on experiment data.
# Create it once with `make data-init data-deploy`; there is deliberately no
# make target that destroys it.

terraform {
  required_version = ">= 1.17.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

variable "region" {
  type    = string
  default = "us-east-2"
}

variable "aws_profile" {
  type    = string
  default = "B10"
}

variable "project" {
  type    = string
  default = "b10"
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile

  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
      Stack     = "data"
    }
  }
}

data "aws_caller_identity" "current" {}

# Bucket names are global across all AWS accounts, so the account ID makes it unique.
resource "aws_s3_bucket" "results" {
  bucket = "${var.project}-results-${data.aws_caller_identity.current.account_id}"
}

# Versioning keeps every earlier copy of an object, so a run file that is
# overwritten or deleted by mistake can still be recovered.
resource "aws_s3_bucket_versioning" "results" {
  bucket = aws_s3_bucket.results.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "results" {
  bucket                  = aws_s3_bucket.results.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

output "results_bucket" {
  value = aws_s3_bucket.results.bucket
}
