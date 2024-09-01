# Lambda function to generate favicon
resource "aws_lambda_function" "favicon_lambda" {
  filename         = "favicon_lambda_package.zip"  # Path to your local ZIP file
  function_name    = "favicon-lambda"
  role             = var.iam_role_arn
  handler          = "lambda_function.lambda_handler"
  runtime          = "python3.10"
  memory_size      = 128
  timeout          = 10
}

# Lambda Function URL
resource "aws_lambda_function_url" "favicon_lambda_url" {
  function_name = aws_lambda_function.favicon_lambda.function_name
  authorization_type = "NONE"  # Set to "NONE" for public access or "AWS_IAM" for IAM-based auth
}

# Optional: Allow public access to the Function URL
resource "aws_lambda_permission" "allow_public_invoke" {
  statement_id  = "AllowPublicInvoke"
  action        = "lambda:InvokeFunctionUrl"
  function_name = aws_lambda_function.favicon_lambda.function_name
  principal     = "*"
  function_url_auth_type = aws_lambda_function_url.favicon_lambda_url.authorization_type
}

output "function_url" {
  value = aws_lambda_function_url.favicon_lambda_url
}
