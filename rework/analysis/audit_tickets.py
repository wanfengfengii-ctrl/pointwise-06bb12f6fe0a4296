"""Center-side audit ticket math over public evidence and companions.

Only V1 evidence (per entry/exit probabilities) and the companion weighted
step counts are available here. The companion value L_e for an entry/exit
edge already equals P(exit) times the expected local step count, so it folds
in the exit probability and must not be multiplied by the edge probability
again. The constructive size-biased walk reproduces the full labelled-path
distribution in AUDIT.md: on every finite qualifying path, each process
position is drawn with mass proportional to the original path probability.
"""
from fractions import Fraction

from rework.analysis.linear_systems import solve_exact_system
from rework.analysis.sampling import categorical
from rework.routing.assignments import TerminalDisposition
from rework.routing.port_checks import PortRef

from .composition import AssessmentGraph


def live_qualifying_entries(graph: AssessmentGraph, start: PortRef) -> set[PortRef]:
    if start not in graph.outgoing:
        raise ValueError('start is not an entry in this assessment')
    incoming: dict[object, set[PortRef]] = {}
    can_pass = set()
    for state, edges in graph.outgoing.items():
        for edge in edges:
            if edge.target is TerminalDisposition.PASS:
                can_pass.add(state)
            elif isinstance(edge.target, PortRef):
                incoming.setdefault(edge.target, set()).add(state)
    queue = list(can_pass)
    while queue:
        state = queue.pop()
        for predecessor in incoming.get(state, ()):
            if predecessor not in can_pass:
                can_pass.add(predecessor)
                queue.append(predecessor)
    reachable = {start}
    queue = [start]
    while queue:
        state = queue.pop()
        for edge in graph.outgoing[state]:
            if isinstance(edge.target, PortRef) and edge.target not in reachable:
                reachable.add(edge.target)
                queue.append(edge.target)
    return can_pass.intersection(reachable)


def reach_and_labeled_steps(
    graph: AssessmentGraph,
    start: PortRef,
    weighted_steps: dict[tuple[PortRef, str], Fraction],
) -> tuple[Fraction, dict[PortRef, Fraction], dict[PortRef, Fraction]]:
    """Total labelled mass at start, per-entry reach probability and step mass."""
    live = live_qualifying_entries(graph, start)
    if start not in live:
        return Fraction(0), {}, {}
    states = sorted(live)
    index = {state: i for i, state in enumerate(states)}
    identity_matrix = [
        [Fraction(i == j) for j in range(len(states))] for i in range(len(states))
    ]

    # Reach probability q(u): standard (I - P_live) q = direct PASS mass.
    reach_matrix = [row[:] for row in identity_matrix]
    reach_rhs = [Fraction(0) for _ in states]
    for i, state in enumerate(states):
        for edge in graph.outgoing[state]:
            if edge.target is TerminalDisposition.PASS:
                reach_rhs[i] += edge.probability
            elif edge.target in index:
                reach_matrix[i][index[edge.target]] -= edge.probability
    reach_values = solve_exact_system(reach_matrix, reach_rhs)
    reach = {state: reach_values[i] for state, i in index.items()}

    # Labelled step mass m(u), with q(PASS) = 1 and q(SCRAP/dead) = 0:
    #   m(u) = sum_e L_e * q(target) + sum_{e to live entry} p_e * m(target)
    # L_e is the companion weighted local step mass (already includes the
    # exit probability), so local terms carry no extra p_e. Future visits
    # transition with the bare edge probability.
    step_matrix = [row[:] for row in identity_matrix]
    step_rhs = [Fraction(0) for _ in states]
    for i, state in enumerate(states):
        for edge in graph.outgoing[state]:
            local_steps = weighted_steps.get((state, edge.exit_port), Fraction(0))
            if edge.target is TerminalDisposition.PASS:
                step_rhs[i] += local_steps
            elif edge.target in index:
                step_matrix[i][index[edge.target]] -= edge.probability
                step_rhs[i] += local_steps * reach[edge.target]
    step_values = solve_exact_system(step_matrix, step_rhs)
    expected_steps = {state: step_values[i] for state, i in index.items()}
    return expected_steps[start], reach, expected_steps


def _current_visit_mass(
    graph: AssessmentGraph,
    state: PortRef,
    reach: dict[PortRef, Fraction],
    weighted_steps: dict[tuple[PortRef, str], Fraction],
) -> Fraction:
    """Marginal mass of marking the current visit at this entry.

    Summed over each public exit: companion weighted local step mass L_e times
    the reach probability of whatever that exit is wired to. q(PASS) = 1 and
    q(SCRAP)/a dead entry = 0.
    """
    mass = Fraction(0)
    for edge in graph.outgoing[state]:
        local_steps = weighted_steps.get((state, edge.exit_port), Fraction(0))
        if edge.target is TerminalDisposition.PASS:
            mass += local_steps
        elif edge.target in reach:
            mass += local_steps * reach[edge.target]
    return mass


def sample_audit_visits(
    graph: AssessmentGraph,
    start: PortRef,
    weighted_steps: dict[tuple[PortRef, str], Fraction],
    randbelow,
) -> tuple[list[tuple[PortRef, str]], int]:
    """Construct one public itinerary and the index of its marked visit.

    Stage A locates the marked visit by its marginal labelled mass: at each
    live entry choose between marking the current visit (mass g(u)) and
    continuing along an edge to a later live entry (mass p_e * m(target)).
    Stage B realizes the full ordered itinerary, sampling the prefix under
    reach-conditioned path probabilities and the suffix under the original
    path probabilities conditioned on eventual PASS. Together every process
    position on every finite qualifying path is drawn in proportion to the
    path's original probability.
    """
    total_mass, reach, expected_steps = reach_and_labeled_steps(graph, start, weighted_steps)
    if total_mass <= 0:
        return [], -1

    def continue_edges(state):
        return [edge for edge in graph.outgoing[state] if edge.target in reach]

    # Stage A: locate the marked visit.
    marked_state = start
    marked_depth = 0
    while True:
        current_mass = _current_visit_mass(graph, marked_state, reach, weighted_steps)
        options = [current_mass]
        edges = continue_edges(marked_state)
        options.extend(edge.probability * expected_steps[edge.target] for edge in edges)
        decision = categorical(options, randbelow)
        if decision == 0:
            break
        marked_state = edges[decision - 1].target
        marked_depth += 1

    # Stage B prefix: walk from start to the marked visit, choosing edges by
    # p_e * q(target), the original path probability conditioned on reaching a
    # qualifying continuation. Record each public exit taken.
    prefix: list[tuple[PortRef, str]] = []
    state = start
    depth = 0
    while depth < marked_depth:
        edges = continue_edges(state)
        weights = [edge.probability * reach[edge.target] for edge in edges]
        edge = edges[categorical(weights, randbelow)]
        prefix.append((state, edge.exit_port))
        state = edge.target
        depth += 1

    # The marked visit itself: draw its public exit by local labelled mass.
    exit_edges = list(graph.outgoing[state])
    exit_weights = []
    for edge in exit_edges:
        local_steps = weighted_steps.get((state, edge.exit_port), Fraction(0))
        if edge.target is TerminalDisposition.PASS:
            exit_weights.append(local_steps)
        elif edge.target in reach:
            exit_weights.append(local_steps * reach[edge.target])
        else:
            exit_weights.append(Fraction(0))
    visits = list(prefix)
    first_edge = exit_edges[categorical(exit_weights, randbelow)]
    visits.append((state, first_edge.exit_port))
    selected = len(visits) - 1

    # Stage B suffix: continue from the marked visit until PASS, using the
    # original edge probabilities conditioned on eventual qualification.
    state = first_edge.target
    while isinstance(state, PortRef):
        edges = graph.outgoing[state]
        weights = []
        for edge in edges:
            if edge.target is TerminalDisposition.PASS:
                weights.append(edge.probability)
            elif edge.target in reach:
                weights.append(edge.probability * reach[edge.target])
            else:
                weights.append(Fraction(0))
        edge = edges[categorical(weights, randbelow)]
        visits.append((state, edge.exit_port))
        state = edge.target
    return visits, selected
