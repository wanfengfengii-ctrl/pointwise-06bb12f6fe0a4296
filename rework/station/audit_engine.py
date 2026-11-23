"""Station-local audit math.

The companion only leaves public ports and aggregate weighted step counts.
Private workstations, raw observations and local paths stay here and are only
replayed during ticket redemption.
"""
from collections import defaultdict, deque
from fractions import Fraction

from rework.analysis.linear_systems import solve_exact_system
from rework.analysis.sampling import categorical
from rework.process.observations import Observation

from .transitions import StationGraph


def _predecessors(graph: StationGraph) -> dict[str, set[str]]:
    incoming: dict[str, set[str]] = defaultdict(set)
    for source, edges in graph.outgoing.items():
        for edge in edges:
            if not edge.external:
                incoming[edge.target].add(source)
    return incoming


def _live_states(exit_name: str, incoming: dict[str, set[str]], graph: StationGraph) -> list[str]:
    states = {
        source for source, edges in graph.outgoing.items()
        if any(edge.external and edge.target == exit_name for edge in edges)
    }
    queue = deque(states)
    while queue:
        for parent in incoming.get(queue.popleft(), ()):
            if parent not in states:
                states.add(parent)
                queue.append(parent)
    return sorted(states)


def _exit_system(graph: StationGraph, exit_name: str, states: list[str]):
    index = {state: i for i, state in enumerate(states)}
    matrix = [[Fraction(i == j) for j in range(len(states))] for i in range(len(states))]
    direct = [Fraction(0) for _ in states]
    for i, state in enumerate(states):
        for edge in graph.outgoing[state]:
            if edge.external:
                if edge.target == exit_name:
                    direct[i] += edge.probability
            elif edge.target in index:
                matrix[i][index[edge.target]] -= edge.probability
    return index, matrix, direct


def _reach_probabilities(graph: StationGraph, exit_name: str, states: list[str]):
    index, matrix, direct = _exit_system(graph, exit_name, states)
    return index, solve_exact_system(matrix, direct)


def weighted_steps_by_exit(graph: StationGraph) -> dict[tuple[str, str], Fraction]:
    """Return weighted local step counts keyed by (public entry, public exit).

    For every finite local path from an entry landing to an exit, sum the path
    probability multiplied by the number of transitions on the path. Zero
    minute transitions and repeated workstations still count as steps.
    """
    incoming = _predecessors(graph)
    landing = dict(graph.entries)
    result: dict[tuple[str, str], Fraction] = {}
    for exit_name in graph.exits:
        states = _live_states(exit_name, incoming, graph)
        if not states:
            continue
        index, reach = _reach_probabilities(graph, exit_name, states)
        step_matrix = [row[:] for row in [
            [Fraction(i == j) for j in range(len(states))] for i in range(len(states))
        ]]
        rhs = []
        for i, state in enumerate(states):
            total = Fraction(0)
            for edge in graph.outgoing[state]:
                if edge.external:
                    if edge.target == exit_name:
                        total += edge.probability
                elif edge.target in index:
                    step_matrix[i][index[edge.target]] -= edge.probability
                    total += edge.probability * reach[index[edge.target]]
            rhs.append(total)
        weighted = solve_exact_system(step_matrix, rhs)
        for entry, workstation in landing.items():
            if workstation in index:
                result[(entry, exit_name)] = weighted[index[workstation]]
    return result


def _total_labelled_mass(
    graph: StationGraph, exit_name: str, states: list[str], index: dict, reach
) -> dict:
    """Total labelled step mass m(u) over exit-paths starting at each state.

    m(u) = q(u) for the current transition plus, on each internal edge that can
    still reach the exit, p_e * m(target). Zero-minute and repeated transitions
    still count as steps.
    """
    matrix = [[Fraction(i == j) for j in range(len(states))] for i in range(len(states))]
    rhs = [Fraction(0) for _ in states]
    for i, state in enumerate(states):
        rhs[i] += reach[index[state]]
        for edge in graph.outgoing[state]:
            if not edge.external and edge.target in index:
                matrix[i][index[edge.target]] -= edge.probability
    values = solve_exact_system(matrix, rhs)
    return {state: values[i] for state, i in index.items()}


def sample_local_audit_path(
    graph: StationGraph, entry: str, exit_name: str, randbelow
) -> tuple[tuple[Observation, ...], int]:
    """Replay one local visit, returning its ordered observations and mark.

    The mark is a zero-based position on the returned tuple. Conditional on the
    chosen entry/exit, every (finite local path reaching the exit, position)
    pair is drawn in proportion to the path's original probability. The walk
    chooses at each state between marking the current transition (mass q(u))
    and continuing through an internal edge (mass p_e * m(target)).
    """
    landing = dict(graph.entries)
    if entry not in landing or exit_name not in graph.exits:
        raise ValueError('audit entry or exit is not declared on this station')
    incoming = _predecessors(graph)
    states = _live_states(exit_name, incoming, graph)
    workstation = landing[entry]
    if workstation not in states:
        raise ValueError('selected visit can never reach its ticket exit')
    index, reach = _reach_probabilities(graph, exit_name, states)
    labelled_mass = _total_labelled_mass(graph, exit_name, states, index, reach)

    def internal_edges(state):
        return [edge for edge in graph.outgoing[state]
                if not edge.external and edge.target in index]

    # Stage one: locate the marked transition. A self-loop can return to the
    # same workstation, so the marked position is tracked by transition depth
    # rather than by node identity. At each state mark the current transition
    # with mass q(u), or continue through an internal edge of mass p_e * m(t).
    marked_state = workstation
    marked_depth = 0
    while True:
        options = [reach[index[marked_state]]]
        edges = internal_edges(marked_state)
        options.extend(edge.probability * labelled_mass[edge.target] for edge in edges)
        decision = categorical(options, randbelow)
        if decision == 0:
            break
        marked_state = edges[decision - 1].target
        marked_depth += 1

    # Stage two prefix: realize the transitions before the marked one under the
    # original path probabilities conditioned on reaching the exit.
    observations: list[Observation] = []
    state = workstation
    for _ in range(marked_depth):
        edges = internal_edges(state)
        weights = [edge.probability * reach[index[edge.target]] for edge in edges]
        edge = edges[categorical(weights, randbelow)]
        observations.append(Observation(state, edge.target, edge.minutes))
        state = edge.target

    # The marked transition at the marked state, including the external exit.
    mark_choices = []
    mark_weights = []
    for edge in graph.outgoing[state]:
        if edge.external:
            if edge.target == exit_name:
                mark_choices.append(edge)
                mark_weights.append(edge.probability)
        elif edge.target in index:
            mark_choices.append(edge)
            mark_weights.append(edge.probability * reach[index[edge.target]])
    marked = len(observations)
    edge = mark_choices[categorical(mark_weights, randbelow)]
    observations.append(Observation(state, edge.target, edge.minutes))

    # Suffix: continue until the external exit under qualifying probabilities.
    state = edge.target
    while not edge.external:
        choices = []
        weights = []
        for next_edge in graph.outgoing[state]:
            if next_edge.external:
                if next_edge.target == exit_name:
                    choices.append(next_edge)
                    weights.append(next_edge.probability)
            elif next_edge.target in index:
                choices.append(next_edge)
                weights.append(next_edge.probability * reach[index[next_edge.target]])
        edge = choices[categorical(weights, randbelow)]
        observations.append(Observation(state, edge.target, edge.minutes))
        state = edge.target
    return tuple(observations), marked
