variable "users" {
  description = "Users to generate passwords for"
  type        = list(string)

  validation {
    condition     = length(var.users) > 0
    error_message = "At least one user is required."
  }
}

variable "path" {
  description = "Secret path in AWS Secrets Manager"
  type        = string
}

variable "password_length" {
  description = "Password length"
  type        = number
  default     = 40
}
