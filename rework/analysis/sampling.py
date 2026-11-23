"""Exact discrete sampling helpers shared by the two independent audit stages."""
from collections.abc import Callable, Sequence
from fractions import Fraction
from math import gcd


def categorical(weights: Sequence[Fraction], randbelow: Callable[[int], int]) -> int:
    """Pick an index with probability proportional to its exact fractional weight."""
    if not weights or any(
        not isinstance(weight, Fraction) or weight < 0 for weight in weights
    ):
        raise ValueError('categorical sampling requires nonnegative exact weights')
    total = sum(weights, Fraction(0))
    if total <= 0:
        raise ValueError('categorical sampling requires positive total weight')
    scale = 1
    for weight in weights:
        scale = scale * weight.denominator // gcd(scale, weight.denominator)
    scaled = [weight.numerator * (scale // weight.denominator) for weight in weights]
    bound = sum(scaled)
    draw = randbelow(bound)
    if type(draw) is not int or not 0 <= draw < bound:
        raise ValueError('randbelow must return an integer inside [0, n)')
    cumulative = 0
    for index, part in enumerate(scaled):
        cumulative += part
        if draw < cumulative:
            return index
    raise ValueError('randbelow draw escaped the weighted range')
