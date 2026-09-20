"""F0 retained-sidecar audit CLI. No pilot collection or training path."""

import argparse

from .retained import audit_retained_sidecar, write_f0_report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sidecar", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    document = audit_retained_sidecar(args.sidecar)
    write_f0_report(args.output, document)
    print(document["outcome"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
