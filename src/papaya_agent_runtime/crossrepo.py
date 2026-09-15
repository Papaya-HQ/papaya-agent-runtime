"""Cross-repository dependency ordering.

A run may span repositories with dependency edges (a task depends on another
landing first). Before delivering, the manager computes a rollout order and
refuses on cycles or dangling edges, so shared-contract changes ship in a safe
sequence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from papaya_agent_runtime.state import init_db, store


class DependencyError(Exception):
    pass


@dataclass
class RolloutPlan:
    order: list[int]
    cycles: list[list[int]] = field(default_factory=list)
    missing: list[tuple[int, int]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.cycles and not self.missing


def _edges(conn, task_ids: set[int]) -> dict[int, set[int]]:
    deps: dict[int, set[int]] = {t: set() for t in task_ids}
    for t in task_ids:
        for dep in store.dependencies_of(conn, t):
            deps[t].add(dep)
    return deps


def rollout_plan(run_id: int) -> RolloutPlan:
    conn = init_db()
    if store.get_run(conn, run_id) is None:
        raise DependencyError(f"run {run_id} not found")
    tasks = store.list_tasks(conn, run_id)
    ids = {t["id"] for t in tasks}
    deps = _edges(conn, ids)

    missing = [(task, dep) for task, ds in deps.items() for dep in ds if dep not in ids]

    # Kahn's algorithm over in-run edges; leftover nodes indicate cycles.
    indeg = {t: 0 for t in ids}
    for t, ds in deps.items():
        for dep in ds:
            if dep in ids:
                indeg[t] += 1
    ready = sorted(t for t, d in indeg.items() if d == 0)
    order: list[int] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for t, ds in deps.items():
            if node in ds and t not in order:
                indeg[t] -= 1
                if indeg[t] == 0:
                    ready.append(t)
        ready.sort()

    cycles: list[list[int]] = []
    if len(order) != len(ids):
        stuck = sorted(ids - set(order))
        cycles.append(stuck)

    return RolloutPlan(order=order, cycles=cycles, missing=missing)
