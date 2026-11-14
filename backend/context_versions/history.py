"""Page-scoped parent DAG queries, independent of SQL and native Git objects."""

from collections import defaultdict, deque
from collections.abc import Iterable
from uuid import UUID

from backend.context_versions.domain import ContextMergeError, VersionNode


class HistoryGraph:
    def __init__(self, nodes: Iterable[VersionNode]) -> None:
        self._nodes: dict[UUID, VersionNode] = {}
        for node in nodes:
            if node.version_id in self._nodes:
                raise ContextMergeError("context_merge_invalid_history")
            self._nodes[node.version_id] = node
        self._ancestors: dict[UUID, frozenset[UUID]] = {}
        self._validate()

    def _validate(self) -> None:
        scope = {(node.workspace_id, node.page_id) for node in self._nodes.values()}
        if len(scope) > 1:
            raise ContextMergeError("context_merge_invalid_history")
        children: dict[UUID, list[UUID]] = defaultdict(list)
        pending = {}
        for node in self._nodes.values():
            if len(set(node.parents)) != len(node.parents):
                raise ContextMergeError("context_merge_invalid_history")
            pending[node.version_id] = len(node.parents)
            for parent in node.parents:
                if parent not in self._nodes:
                    raise ContextMergeError("context_merge_invalid_history")
                children[parent].append(node.version_id)
        ready = deque(key for key, count in pending.items() if count == 0)
        visited = 0
        while ready:
            visited += 1
            for child in children[ready.popleft()]:
                pending[child] -= 1
                if pending[child] == 0:
                    ready.append(child)
        if visited != len(self._nodes):
            raise ContextMergeError("context_merge_invalid_history")

    def node(self, version_id: UUID) -> VersionNode:
        try:
            return self._nodes[version_id]
        except KeyError as exc:
            raise ContextMergeError("context_merge_invalid_history") from exc

    def parents(self, version_id: UUID) -> tuple[UUID, ...]:
        return self.node(version_id).parents

    def ancestors(self, version_id: UUID) -> frozenset[UUID]:
        if version_id not in self._ancestors:
            visited: set[UUID] = set()
            pending = [version_id]
            while pending:
                current = pending.pop()
                if current not in visited:
                    visited.add(current)
                    pending.extend(self.parents(current))
            self._ancestors[version_id] = frozenset(visited)
        return self._ancestors[version_id]

    def is_ancestor(self, ancestor: UUID, descendant: UUID) -> bool:
        self.node(ancestor)
        return ancestor in self.ancestors(descendant)

    def merge_bases(self, ours: UUID, theirs: UUID) -> tuple[UUID, ...]:
        common = self.ancestors(ours) & self.ancestors(theirs)
        # Common ancestry is downward closed: every dominated node is an
        # immediate parent of another common node along a path to a best base.
        dominated = {parent for key in common for parent in self.parents(key)}
        return tuple(sorted(common - dominated, key=str))

    def with_node(self, node: VersionNode) -> "HistoryGraph":
        return HistoryGraph((*self._nodes.values(), node))
