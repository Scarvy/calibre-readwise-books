"""Calibre plugin registration for Readwise Books.

The working code lives in ui.py and the bundled calibre_bookwise modules;
Calibre loads this file first and imports actual_plugin only in the GUI.
"""

from typing import ClassVar

from calibre.customize import InterfaceActionBase


class ReadwiseBooksPlugin(InterfaceActionBase):
    name = "Readwise Books"
    description = (
        "Records reading in Calibre's e-book viewer as reading sessions for "
        "Readwise Books. v0 records only: nothing is sent anywhere."
    )
    supported_platforms: ClassVar[list[str]] = ["osx", "windows", "linux"]
    author = "Scott Carvalho"
    version = (0, 1, 0)
    minimum_calibre_version = (8, 0, 0)
    actual_plugin = "calibre_plugins.readwise_books.ui:ReadwiseBooksAction"
