resource "random_password" "user_password" {
  for_each = toset(var.users)
  length   = var.password_length
  special  = true
}

resource "aws_secretsmanager_secret" "this" {
  name                    = var.path
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "trino_passwords_version" {
  secret_id = aws_secretsmanager_secret.this.id
  secret_string = join("\n", [
    for user in var.users : "${user}=${random_password.user_password[user].result}"
  ])

  lifecycle {
    ignore_changes = [secret_string]
  }
}

moved {
  from = aws_secretsmanager_secret.trino_passwords
  to   = aws_secretsmanager_secret.this
}
