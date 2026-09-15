"""Enable `python -m papaya_agent_runtime` as the `ppy` entry point."""

from papaya_agent_runtime.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
