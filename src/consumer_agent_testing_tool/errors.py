class LabError(Exception):
    """An actionable failure with stable CLI semantics."""

    def __init__(self, message: str, *, code: int = 2, category: str = "setup/infrastructure"):
        super().__init__(message)
        self.code = code
        self.category = category


class Unsupported(LabError):
    def __init__(self, message: str):
        super().__init__(message, code=3, category="unsupported")


class LimitExhausted(LabError):
    """The agent used its turn, tool-call or time budget without finishing: a failed trial."""

    def __init__(self, message: str):
        super().__init__(message, code=1, category="interaction")


def normalize_error(error: Exception) -> LabError:
    if isinstance(error, LabError):
        return error
    if isinstance(error, ExceptionGroup):
        leaves = [normalize_error(child) for child in error.exceptions if isinstance(child, Exception)]
        if leaves:
            # Preserve the real cause through SDK task-group wrappers.
            return next((leaf for leaf in leaves if leaf.code != 2), leaves[0])
    if getattr(getattr(error, "response", None), "status_code", None) in (401, 403):
        return LabError("Target authentication rejected; verify its separate credential and scope",
                        category="authentication")
    if isinstance(error, (OSError, ValueError)):
        return LabError(f"{type(error).__name__}: {error}")
    return LabError(f"{type(error).__name__}: execution failed; inspect local evidence")
