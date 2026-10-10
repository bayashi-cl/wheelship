class WheelshipError(Exception):
    """An error reported to the user without a traceback."""


class ConfigError(WheelshipError):
    pass


class ResolveError(WheelshipError):
    pass


class FetchError(WheelshipError):
    pass


class BuildError(WheelshipError):
    pass
