resource "aws_ecr_repository" "fake_models" {
  name                 = "services/fake-models"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}
