variable "vpc_id" {
  type = string
}

variable "subnets" {
  description = "Map of subnet name to cidr and az"
  type        = map(object({ cidr = string, az = string }))
}

variable "tags" {
  description = "Tags applied to all resources"
  type        = map(string)
  default = {}
}

variable "map_public_ip" {
  type    = bool
  default = false
}

variable "enable_ipv6" {
  type    = bool
  default = false
}
