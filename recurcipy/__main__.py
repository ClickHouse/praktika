import argparse

from recurcipy.yaml_generator import YamlGenerator


def parse_args():
    parser = argparse.ArgumentParser("RecurCIPY")
    parser.add_argument(
        "--generate",
        action="store_true",
        help="Generates CI pipeline in accordance with configs in ./ci/configs/*.py",
    )
    parser.add_argument(
        "--generate-from-example",
        type=str,
        default="",
        help="--generate-from-example <EXAMPLE>. Generates CI pipeline from <EXAMPLE>",
    )
    return parser.parse_args(), parser


if __name__ == '__main__':
    args, parser = parse_args()

    if args.generate_from_example:
        YamlGenerator().generate_from_example(example=args.generate_from_example)
    elif args.generate:
        YamlGenerator().generate()
    else:
        assert False
