import datetime
import os
import traceback

import bpy


LOG_NAME = "ISSMAKER_Modeling_Assistant.log"


def path():
    root = bpy.app.tempdir or os.path.abspath(os.path.expanduser("~"))
    return os.path.join(root, LOG_NAME)


def initialize():
    try:
        with open(path(), "a", encoding="utf-8") as stream:
            stream.write("\n" + "=" * 72 + "\n")
            stream.write(f"Session {datetime.datetime.now().isoformat(timespec='seconds')} | Blender {bpy.app.version_string}\n")
    except Exception:
        pass


def write(message):
    try:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path(), "a", encoding="utf-8") as stream:
            stream.write(f"[{stamp}] {message}\n")
    except Exception:
        pass


def write_exception(message, exc):
    write(f"ERROR: {message}: {exc}\n{traceback.format_exc()}")

