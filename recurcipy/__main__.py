import argparse

from recurcipy.validator import Validator
from recurcipy.yaml_generator import YamlGenerator


def parse_args():
    parser = argparse.ArgumentParser("RecurCIPY")
    parser.add_argument(
        "--generate",
        action="store_true",
        help="Generates CI pipeline in accordance with configs in ./ci/configs/*.py",
    )
    return parser.parse_args(), parser


if __name__ == "__main__":
    args, parser = parse_args()

    if args.generate:
        Validator().validate()
        YamlGenerator().generate()
    else:
        assert False
