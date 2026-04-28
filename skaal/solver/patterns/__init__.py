"""Built-in pattern solvers.

Importing this package registers the default pattern solver functions.
"""

from skaal.solver.patterns import event_log, outbox, projection, saga

__all__ = ["event_log", "projection", "saga", "outbox"]