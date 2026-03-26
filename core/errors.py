"""Shared runtime errors for PHANTOM."""


class PhantomError(Exception):
    """Base runtime error."""


class BudgetExceeded(PhantomError):
    """Raised when a run crosses its configured budget."""


class CheckpointDeclined(PhantomError):
    """Raised when a human checkpoint rejects an action."""


class CriticEscalation(PhantomError):
    """Raised when the critic blocks progress repeatedly."""
