"""Calibre plugin registration for Bookwise Sessions.

The working code lives in ui.py and the bundled calibre_bookwise modules;
Calibre loads this file first and imports actual_plugin only in the GUI.
"""

from typing import ClassVar

from calibre.customize import InterfaceActionBase


class BookwiseSessionsPlugin(InterfaceActionBase):
    name = "Bookwise Sessions"
    description = (
        "Records reading in Calibre's e-book viewer as reading sessions for "
        "Readwise Bookwise. v0 records only: nothing is sent anywhere."
    )
    supported_platforms: ClassVar[list[str]] = ["osx", "windows", "linux"]
    author = "Scott Carvalho"
    version = (0, 1, 0)
    minimum_calibre_version = (8, 0, 0)
    actual_plugin = "calibre_plugins.bookwise_sessions.ui:BookwiseSessionsAction"
