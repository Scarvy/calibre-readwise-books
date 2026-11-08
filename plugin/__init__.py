"""Calibre plugin registration for Readwise Books.

The working code lives in ui.py and the bundled calibre_bookwise modules;
Calibre loads this file first and imports actual_plugin only in the GUI.

Import nothing else from this plugin here. Installing over an existing copy
re-runs only this file; any plugin module it imported would still be the old
copy's, cached when Calibre started.
"""

from typing import ClassVar

from calibre.customize import InterfaceActionBase

IS_DEV = True  # scripts/build_plugin.py writes False here for release zips


class ReadwiseBooksPlugin(InterfaceActionBase):
    name = "Readwise Books"
    description = (
        "Records reading in Calibre's e-book viewer as reading sessions for "
        "Readwise Books, with opt-in session, highlight, and EPUB uploads."
        + (" Development build." if IS_DEV else " Release build.")
    )
    supported_platforms: ClassVar[list[str]] = ["osx", "windows", "linux"]
    author = "Scott Carvalho"
    version = (0, 2, 0)
    minimum_calibre_version = (8, 0, 0)
    actual_plugin = "calibre_plugins.readwise_books.ui:ReadwiseBooksAction"
