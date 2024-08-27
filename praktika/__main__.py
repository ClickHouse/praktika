import argparse

from praktika.validator import Validator
from praktika.yaml_generator import YamlGenerator


def parse_args():
    parser = argparse.ArgumentParser("praktika")
    parser.add_argument(
        "--generate",
        action="store_true",
        help="Generates CI pipeline in accordance with configs in ./ci/configs/*.py",
    )
    parser.add_argument(
        "--workflow",
        default="",
        type=str,
        help="Select specific workflow from ./ci/configs/*.py",
    )
    return parser.parse_args(), parser


if __name__ == "__main__":
    args, parser = parse_args()

    if args.generate:
        Validator().validate()
        YamlGenerator().generate(args.workflow)
    else:
        assert False
