variable "subnet_id" {
  type = string
}

variable "tags" {
  type = map(string)
}

resource "aws_eip" "nat" {
  domain = "vpc"
}

resource "aws_nat_gateway" "this" {
  allocation_id = aws_eip.nat.id
  subnet_id     = var.subnet_id
  tags          = var.tags
}

output "id" {
  value = aws_nat_gateway.this.id
}
