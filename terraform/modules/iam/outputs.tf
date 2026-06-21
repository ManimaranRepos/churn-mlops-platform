output "eks_node_role_arn" {
  value = aws_iam_role.eks_node.arn
}

output "eks_node_role_name" {
  value = aws_iam_role.eks_node.name
}

output "eks_node_instance_profile_arn" {
  value = aws_iam_instance_profile.eks_node.arn
}

output "sagemaker_role_arn" {
  value = aws_iam_role.sagemaker.arn
}

output "glue_role_arn" {
  value = aws_iam_role.glue.arn
}

output "lambda_role_arn" {
  value = aws_iam_role.lambda.arn
}

output "cicd_role_arn" {
  value       = aws_iam_role.cicd.arn
  description = "Paste this into your GitHub Actions workflow as the role-to-assume"
}
