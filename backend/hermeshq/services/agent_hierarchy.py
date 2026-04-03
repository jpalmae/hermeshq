from fastapi import HTTPException

from hermeshq.models.agent import Agent


def ancestor_chain(agent_map: dict[str, Agent], agent_id: str) -> list[str]:
    chain: list[str] = []
    seen: set[str] = set()
    current = agent_map.get(agent_id)
    parent_id = current.supervisor_agent_id if current else None
    while parent_id and parent_id not in seen:
        chain.append(parent_id)
        seen.add(parent_id)
        parent = agent_map.get(parent_id)
        parent_id = parent.supervisor_agent_id if parent else None
    return chain


def descendant_ids(agent_map: dict[str, Agent], root_id: str) -> set[str]:
    children_by_parent: dict[str | None, list[str]] = {}
    for agent in agent_map.values():
        children_by_parent.setdefault(agent.supervisor_agent_id, []).append(agent.id)
    descendants: set[str] = set()
    stack = list(children_by_parent.get(root_id, []))
    while stack:
        current = stack.pop()
        if current in descendants:
            continue
        descendants.add(current)
        stack.extend(children_by_parent.get(current, []))
    return descendants


def delegate_route(agent_map: dict[str, Agent], source: Agent, target: Agent) -> tuple[bool, str]:
    if source.id == target.id:
        return False, "self"
    if not source.can_send_tasks:
        return False, "source_blocked"
    if not target.can_receive_tasks:
        return False, "target_blocked"
    if not source.supervisor_agent_id:
        return True, "independent"

    ancestor_ids = set(ancestor_chain(agent_map, source.id))
    if target.id in ancestor_ids:
        return True, "upward"

    descendants = descendant_ids(agent_map, source.id)
    if target.id in descendants:
        return True, "downward"

    return False, "cross_branch"


def validate_delegate_hierarchy(agent_map: dict[str, Agent], source: Agent, target: Agent) -> None:
    allowed, route = delegate_route(agent_map, source, target)
    if allowed:
        return
    if route == "self":
        raise HTTPException(status_code=400, detail="An agent cannot delegate a task to itself")
    if route == "source_blocked":
        raise HTTPException(status_code=400, detail=f"{source.friendly_name or source.name} cannot send delegated tasks")
    if route == "target_blocked":
        raise HTTPException(status_code=400, detail=f"{target.friendly_name or target.name} cannot receive delegated tasks")
    raise HTTPException(
        status_code=400,
        detail=(
            "Delegation violates hierarchy. Subordinates may escalate upward to supervisors or delegate downward within "
            "their own branch. Cross-branch lateral delegation is blocked."
        ),
    )


def route_label(route: str) -> str:
    return {
        "independent": "independent",
        "upward": "escalate upward",
        "downward": "delegate downward",
        "cross_branch": "cross-branch blocked",
        "source_blocked": "source cannot send tasks",
        "target_blocked": "target cannot receive tasks",
        "self": "self blocked",
    }.get(route, route)
