"""Exceptions shared across modules."""


class ConfigError(ValueError):
    """A configuration file (users, providers and models) is malformed."""


class InvalidInput(ValueError):
    """The client sent something unusable. The message is shown to the user."""


class NotFound(Exception):
    """No such job (or image) for this user (404). The message is shown to the user."""


class Conflict(Exception):
    """The request does not fit the job's current state (409). The message is shown to the user."""


class TooManyJobs(Exception):
    """The user has as many jobs in progress as allowed (429). The message is shown to the user."""
