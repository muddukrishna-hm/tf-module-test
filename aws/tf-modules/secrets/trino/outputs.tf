output "secret_name" {
  value = aws_secretsmanager_secret.trino_passwords.name
}
