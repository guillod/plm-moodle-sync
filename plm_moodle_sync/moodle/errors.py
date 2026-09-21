"""Errors reported by Moodle authentication, course inspection, and publication."""


class MoodleError(RuntimeError):
    """A Moodle operation failed; messages contain no session secrets."""
