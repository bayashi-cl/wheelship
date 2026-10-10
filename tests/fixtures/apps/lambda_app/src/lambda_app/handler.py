import importlib.metadata
import sys
from pathlib import Path

import markupsafe
from markupsafe import _speedups


def handler(event, context):
    return {
        "escaped": str(markupsafe.escape(event.get("text", "<b>hi</b>"))),
        "native_ext": Path(_speedups.__file__).name,
        "attrs": importlib.metadata.version("attrs"),
        "python": "%d.%d" % sys.version_info[:2],
    }
