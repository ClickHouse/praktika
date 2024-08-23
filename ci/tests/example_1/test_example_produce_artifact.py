import unittest

from praktika.utils import Shell
from praktika.settings import Settings


class TestExample1(unittest.TestCase):
    def test_example_1(self):
        with open(f"{Settings.OUTPUT_DIR}/hello_world.txt", "w", encoding="utf-8") as f:
            f.write("Hello World!\n")
            f.flush()
        Shell.check(f"cat {Settings.OUTPUT_DIR}/hello_world.txt")
