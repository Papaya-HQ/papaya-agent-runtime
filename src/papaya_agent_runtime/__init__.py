"""Papaya Agent Runtime control plane.

The home a Papaya connected agent is pointed at to build code: it directs autonomous
software work across one or more Git repositories, and takes its identity, rules and
memories from the Papaya agent this machine is connected as. See
docs/runtime-contract.md for the runtime contract and AGENTS.md for the development
contract.
"""


def __getattr__(name: str) -> str:
    """`__version__`, read from the checkout's tags the first time anybody asks.

    A module-level `__getattr__` rather than an assignment, because deriving the
    version costs a subprocess and importing this package costs nothing today:
    every `ppy` command, every test module and every `import papaya_agent_runtime`
    anywhere would otherwise pay for a `git describe` it may never read. The
    answer is remembered in `version.py`, so the cost is at most once per process
    and only for a process that actually asks.

    See `version.py` for where the number comes from and why the tag is the
    source of it.
    """
    if name == "__version__":
        from papaya_agent_runtime.version import version

        return version()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*globals(), "__version__"])
