output "subnet_ids" {
  value = { for k, s in aws_subnet.private : k => s.id }
}

output "route_table_id" {
  value = aws_route_table.main.id
}

output "subnet_cidrs" {
  value = { for k, s in aws_subnet.private : k => s.cidr_block }
}
