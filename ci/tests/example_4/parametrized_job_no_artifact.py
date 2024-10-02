from praktika.environment import Environment
from praktika.result import Result

if __name__ == "__main__":

    print(f"Job Parameter: {Environment.PARAMETER}")

    Result.get().set_success()
