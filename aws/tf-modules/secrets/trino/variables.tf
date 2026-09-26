variable "users" {
  description = "Users to generate passwords for"
  type        = list(string)
}

variable "path" {
  description = "Secret path in AWS Secrets Manager"
  type        = string
}

variable "password_length" {
  description = "Password length"
  type        = number
  default     = 32
}
