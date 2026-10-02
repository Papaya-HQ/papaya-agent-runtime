"""`ppy serve --cloud`: this runtime as a runner Papaya hosts on a cloud provider (PAP-334).

A person can ask Papaya to run their engineer in the cloud instead of on their
own machine. Papaya provisions a VM on a provider (Maritime first) from this
repository's image (`docker/Dockerfile`), with the connection's `pagc_` token
as a secret environment variable, and the VM runs `ppy serve --cloud`. From
there it is the same runtime a laptop runs, pointed at the same checkout, with
three differences, all of them the client's (`papaya_agent_client.cloud_host`):

- **Connected from the environment.** The token is taken out of the environment
  before anything else starts (:func:`take_start`), so no worker or harness
  inherits it, and stored in the client home under `/data` like any connection,
  with this checkout as its working directory (:func:`open_host`).
- **One HTTP port for the provider.** `GET /health`; `POST /chat`, the doorbell
  Papaya rings when there is work, which only wakes the listener's pull; and
  `/terminal`, the sign-in terminal.
- **The sign-in terminal.** The owner opens it from Papaya with a one-time
  ticket and runs `ppy setup` there, the same steps as on a laptop: signing
  Claude Code (or Codex) and GitHub in with their own accounts, and choosing the
  repositories. Nothing typed passes through Papaya; the browser talks to the
  VM. Until that is done the runner says what is missing the way any machine
  does, as a blocker in the owner's DM.

Everything that must survive the VM sleeping lives under `/data`: the client
home, `PPY_HOME`, and `HOME` itself, where the CLIs keep their sign-ins.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The checkout this runtime runs from; in the image, `/opt/papaya-agent-runtime`.
CHECKOUT = Path(__file__).resolve().parents[2]
#: Where the persistent disk is mounted. Overridable for a local run.
DATA_DIR_ENV = "PPY_CLOUD_DATA_DIR"
#: Which harness the runner was created for (`claude-code` or `codex`), set by Papaya.
HARNESS_ENV = "PAPAYA_AGENT_HARNESS"
DEFAULT_HARNESS = "claude-code"


@dataclass(frozen=True)
class CloudStart:
    """What `serve` took from the environment before anything else ran."""

    token: str = field(repr=False)
    harness: str
    data_dir: str


def take_start(env: MutableMapping[str, str] | None = None) -> CloudStart:
    """Point the client home under the data disk and take the token out of the environment.

    Called first thing, before the supervisor or anything else that starts a
    process, so that nothing this serve starts can inherit the token. Raises
    `papaya_agent_client.cloud_host.CloudHostError` when there is no token.
    """
    from papaya_agent_client import cloud_host

    env = os.environ if env is None else env
    data_dir = env.get(DATA_DIR_ENV) or cloud_host.DEFAULT_DATA_DIR
    cloud_host.prepare_environment(data_dir)
    token = cloud_host.take_token(env)
    harness = (env.get(HARNESS_ENV) or DEFAULT_HARNESS).strip() or DEFAULT_HARNESS
    return CloudStart(token=token, harness=harness, data_dir=data_dir)


def terminal_command(checkout: Path = CHECKOUT) -> list[str]:
    """What the sign-in terminal runs: setup, then a shell for anything else.

    `ppy setup` is the same guided setup a person runs on a laptop; it checks
    each step first, so opening the terminal again on a set-up runner says
    everything is in place.
    """
    launcher = shlex.quote(str(checkout / "bin" / "ppy"))
    return ["bash", "-l", "-c", f"{launcher} setup; exec bash -l"]


#: The client's `connect_home`, as a seam for tests.
Connect = Callable[..., Awaitable[dict[str, Any]]]


async def open_host(
    start: CloudStart,
    *,
    port: int | None = None,
    connect: Connect | None = None,
    checkout: Path = CHECKOUT,
) -> Any:
    """Connect the client home from the token and start the provider's HTTP port.

    Returns the started `CloudHost`; `serve` attaches the listener to it once
    the listener is built, and closes it on the way out.
    """
    from papaya_agent_client import cloud_host
    from papaya_agent_client.cloud_terminal import TerminalService

    connect = connect or cloud_host.connect_home
    identity = await connect(
        start.token,
        harness="codex" if start.harness == "codex" else "claude",
        working_directory=checkout,
    )
    agent_id = str((identity.get("agent") or {}).get("id") or "")
    terminal = TerminalService(
        terminal_command(checkout),
        cwd=str(checkout),
        redeem=cloud_host.redeemer(agent_id),
    )
    if port is None:
        port = int(os.environ.get(cloud_host.PORT_ENV) or cloud_host.DEFAULT_PORT)
    host = cloud_host.CloudHost(
        port=port,
        terminal=terminal,
        invoke_url=cloud_host.invoke_url_from_env(),
    )
    await host.start()
    return host


__all__ = [
    "CHECKOUT",
    "DATA_DIR_ENV",
    "HARNESS_ENV",
    "CloudStart",
    "open_host",
    "take_start",
    "terminal_command",
]
