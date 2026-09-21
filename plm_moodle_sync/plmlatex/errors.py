"""Errors suitable for display without exposing authentication data."""


class CompilationError(RuntimeError):
    """A project could not be queried, compiled, or downloaded."""


class AuthenticationError(CompilationError):
    """A saved session is missing, invalid, or could not be renewed."""
