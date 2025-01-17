from praktika.info import Info

if __name__ == "__main__":
    print("User Name", Info.get_workflow_input_value("user_name"))
    print("User Age", Info.get_workflow_input_value("user_age"))
    print("NA", Info.get_workflow_input_value("invalid_name"))
