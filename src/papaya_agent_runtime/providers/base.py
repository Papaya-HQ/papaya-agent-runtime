"""The provider adapter contract shared by fake, Claude, and Codex adapters."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field


@dataclass
class TaskSpec:
    """A bounded unit of work handed to a worker."""

    task_id: int
    title: str
    instructions: str
    worktree_path: str
    base_sha: str
    provider: str
    model: str | None = None
    reasoning: str | None = None
    run_id: int | None = None
    resume_session_id: str | None = None
    steer_message: str | None = None
    # The lease branch the worker pushes to. Set by the supervisor at dispatch;
    # provider command rules name it so the worker never has to guess.
    branch: str | None = None
    # Repo-memory preamble prepended to a fresh worker's prompt so it consults and
    # updates the repo's shared notes/tasks. Set by the supervisor at dispatch.
    memory_preamble: str | None = None
    # The per-repository environment block (evidence directory, local gate,
    # private database stack, push-hook policy). Rendered by the supervisor at
    # dispatch from repo config; the adapters place it with the command rules.
    environment: str | None = None
    # Resolved task-scoped values overlaid on the child process environment.
    process_env: dict[str, str] = field(default_factory=dict)


@dataclass
class UsageInfo:
    provider: str
    model: str | None = None
    reasoning: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class ProviderEvent:
    """A normalized event parsed from a provider's structured stream."""

    kind: str  # session | assistant | tool | progress | result | usage | error
    raw: dict = field(default_factory=dict)
    session_id: str | None = None
    text: str | None = None


@dataclass
class WorkerResult:
    status: str  # completed | blocked | failed
    summary: str = ""
    session_id: str | None = None
    usage: UsageInfo | None = None
    question: str | None = None  # populated when status == "blocked"
    head_sha: str | None = None


class ProviderAdapter(abc.ABC):
    """Encapsulates command construction, event parsing, and resume behavior."""

    name: str

    @abc.abstractmethod
    def probe(self) -> dict:
        """Return the capability record for this provider/version (fail-closed)."""

    def worker_prompt(self, spec: TaskSpec) -> str:
        """The prompt for a fresh worker: repo-memory preamble (if any) + the task.

        Real adapters use this so a worker is pointed at the repo's shared
        notes/tasks; the fake adapter keeps raw ``instructions`` for its markers.
        """
        if spec.memory_preamble:
            return f"{spec.memory_preamble}\n\n---\n\n{spec.instructions}"
        return spec.instructions

    @abc.abstractmethod
    def start(self, spec: TaskSpec) -> list[str]:
        """Build the argv to start a fresh worker for ``spec``."""

    @abc.abstractmethod
    def resume(self, spec: TaskSpec) -> list[str]:
        """Build the argv to resume ``spec`` by its session id."""

    def steer(self, spec: TaskSpec, message: str) -> list[str] | None:
        """Return argv to steer an active/resumed worker, or None if unsupported.

        Default: checkpoint steering via a resume with the steer message. Only
        adapters whose capability flags prove mid-flight steering override this
        to inject into a running process.
        """
        spec.steer_message = message
        return self.resume(spec)

    def interrupt_signal(self) -> int:
        """Signal to deliver to the process group for a cooperative stop."""
        import signal

        return int(signal.SIGINT)

    @abc.abstractmethod
    def reconcile(self, spec: TaskSpec) -> dict:
        """Return a compact recovery packet for a half-alive/dropped worker."""

    @abc.abstractmethod
    def parse_event(self, line: str) -> ProviderEvent | None:
        """Parse one structured output line into a normalized event, or None."""

    def permission_denials(self, events: list[ProviderEvent]) -> list[dict]:
        """Tool calls the harness refused this turn (``tool_name``, ``tool_input``).

        Only a harness that reports them structurally overrides this; the runtime
        learns tools from them (`tool_learning`).
        """
        return []

    def live_denial(self, event: ProviderEvent, events: list[ProviderEvent]) -> dict | None:
        """The denial ``event`` reports as it happens, shaped like one of
        :meth:`permission_denials`, or None. ``events`` are the turn's events so far.

        The turn's list only arrives when the session ends; a steer about a denial is
        worth something while the worker is still running.
        """
        return None

    @abc.abstractmethod
    def parse_usage(self, events: list[ProviderEvent]) -> UsageInfo | None:
        """Extract usage accounting from a sequence of parsed events."""

    @abc.abstractmethod
    def result(self, events: list[ProviderEvent], exit_code: int | None) -> WorkerResult:
        """Derive the final worker result. Never infer success from a missing process."""
