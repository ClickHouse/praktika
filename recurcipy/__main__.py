import argparse

from recurcipy.yaml_generator import YamlGenerator


def parse_args():
    parser = argparse.ArgumentParser("RecurCIPY")
    parser.add_argument(
        "--generate",
        action="store_true",
        help="Generates YAML workflow in acordance to config in ./ci/workflows.py",
    )
    parser.add_argument(
        "--hello-world",
        action="store_true",
        help="Generates Hello World example",
    )
    return parser.parse_args(), parser


if __name__ == '__main__':
    args, parser = parse_args()

    if args.hello_world:
        YamlGenerator().hello_world()
    elif args.generate:
        YamlGenerator().generate()
    else:
        assert False
