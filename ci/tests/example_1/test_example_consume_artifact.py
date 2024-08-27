import unittest
from praktika.settings import Settings


class TestExample1(unittest.TestCase):
    def test_example_1(self):
        with open(f"{Settings.INPUT_DIR}/hello_world.txt", "r", encoding="utf-8") as f:
            self.assertEqual(f.readline().strip(), "Hello World!")
