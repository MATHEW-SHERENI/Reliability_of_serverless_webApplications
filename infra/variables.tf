variable "region" {
  description = "AWS region for every resource (fixed in the design, Table 3)."
  type        = string
  default     = "us-east-2"
}

variable "aws_profile" {
  description = "Named AWS CLI profile that holds the credentials."
  type        = string
  default     = "B10"
}

variable "project" {
  description = "Prefix for resource names and value of the Project tag."
  type        = string
  default     = "b10"
}
