"""Packaging entry point for PHANTOM."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def main() -> None:
    script = Path(__file__).with_name("phantom.py")
    spec = spec_from_file_location("phantom_main", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load PHANTOM entrypoint from {script}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main()
