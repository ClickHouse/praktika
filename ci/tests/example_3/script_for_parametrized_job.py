from praktika.environment import Environment
from praktika.result import Result

if __name__ == "__main__":

    print(Environment.PARAMETER)

    try:
        print(Environment.PARAMETER.name)
    except:
        print("parameter is not a json object")

    Result.get().set_success()
