data "local_file" "user_data" {
  filename = "${path.module}/user_data.txt"
}

data "local_file" "user_data_fixed_size_asg" {
  filename = "${path.module}/fixed_size_user_data.txt"
}
