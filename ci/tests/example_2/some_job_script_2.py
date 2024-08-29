from praktika.result import Result

if __name__ == "__main__":
    # 1. do some work
    #   ...

    # 2. dump results
    Result.get().set_success()  # just set success status, without any other info
