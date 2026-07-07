# infra/terraform/secrets.tf
#
# Third-party API keys the app optionally uses, stored in Secrets Manager rather
# than passed as raw shell env vars into bootstrap. Each resource is conditional
# on a value actually being supplied in terraform.tfvars, so leaving a key unset
# is a legitimate, supported no-op — the corresponding secret simply isn't
# created and the app feature that needs it degrades gracefully.

resource "aws_secretsmanager_secret" "tavily_api_key" {
  count       = var.tavily_api_key != "" ? 1 : 0
  name        = "${var.cluster_name}/tavily-api-key"
  description = "Tavily Search API key for the RAG agent's web_search tool"
}

resource "aws_secretsmanager_secret_version" "tavily_api_key" {
  count         = var.tavily_api_key != "" ? 1 : 0
  secret_id     = aws_secretsmanager_secret.tavily_api_key[0].id
  secret_string = var.tavily_api_key
}
