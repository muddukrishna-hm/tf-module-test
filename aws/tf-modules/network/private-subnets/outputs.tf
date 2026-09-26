output "subnet_ids" {
  value = { for k, s in aws_subnet.private : k => s.id }
}
