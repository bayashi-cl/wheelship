class PyboxError(Exception):
    """An error reported to the user without a traceback."""


class ConfigError(PyboxError):
    pass


class ResolveError(PyboxError):
    pass


class FetchError(PyboxError):
    pass


class BuildError(PyboxError):
    pass
