from markupsafe import escape


def greeting() -> str:
    return str(escape("<hello from the workspace>"))
