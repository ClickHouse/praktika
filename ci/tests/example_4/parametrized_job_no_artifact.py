from praktika.environment import Environment
from praktika.result import Result


if __name__ == "__main__":

    try:
        for key, value in vars(Environment.PARAMETER).items():
            print(f"{key}: {value}")
    except:
        print(f"parameter is not a json object: {Environment.PARAMETER}")

    Result.get().set_success()
