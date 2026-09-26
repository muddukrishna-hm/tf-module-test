variable "vpc_id" {
  type = string
}

variable "subnets" {
  description = "Map of subnet name to cidr and az"
  type        = map(object({ cidr = string, az = string }))
}

variable "tags" {
  type    = map(string)
  default = {}
}
