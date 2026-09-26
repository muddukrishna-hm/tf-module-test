resource "aws_subnet" "private" {
  for_each          = var.subnets
  vpc_id            = var.vpc_id
  cidr_block        = each.value.cidr
  availability_zone = each.value.az
  tags              = merge(var.tags, { Name = "private-${each.key}" })
}

resource "aws_route_table" "main" {
  vpc_id = var.vpc_id
  tags   = var.tags
}
