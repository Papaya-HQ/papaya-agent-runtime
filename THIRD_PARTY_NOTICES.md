# Third-party notices

Papaya Agent Runtime uses the following companion tools as pinned dependencies. Their
source trees are not vendored into this repository; each is used from PATH when
present (managed provisioning into the gitignored `.ppy/tools/` directory is a
follow-up). Pins and integrity metadata are recorded in
[`tools.lock`](tools.lock).

- **Treehouse** `v2.3.0` — pre-warmed, reusable, identity-bound Git worktree
  leases. Upstream: <https://github.com/kunchenguid/treehouse>. License: MIT.
- **lavish-axi** `0.1.62` — agent-first rich HTML review surfaces and structured
  feedback. Upstream: <https://github.com/kunchenguid/lavish-axi>. License: MIT.
- **gh-axi** `0.1.34` — token-efficient GitHub operations for agents. Upstream:
  npm `gh-axi`. License: MIT.

All three are MIT-licensed; the full upstream license texts are available at the
sources above and are reproduced with each distributed artifact.

The runtime Python control plane itself has zero third-party package
dependencies. Development uses Ruff and pytest (see `pyproject.toml`).
