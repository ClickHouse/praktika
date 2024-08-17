variable "region" {
  default = "us-east-1"
}

variable "iam_role_arn" {
  default = "arn:aws:iam::542516086801:role/lambda_admin"
}

variable "sg_id" {
  default = "sg-04d02f0265bee8b24"
}

variable "ami_id" {
  default = "ami-015989dfbaffe7838"
}

variable "key_name" {
  default = "maxkey"
}

variable "runner_small" {
  default = "maxs-small"
}