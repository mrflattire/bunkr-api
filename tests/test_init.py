"""
Pins down the public surface of the bunkr_api package itself:
importing bunkr_api should not require reaching into submodules.
"""

import bunkr_api


def test_bunkr_api_is_exported_at_top_level():
    """BunkrAPI should be importable directly from the package root."""
    from bunkr_api import BunkrAPI

    assert bunkr_api.BunkrAPI is BunkrAPI


def test_version_is_exposed():
    """__version__ should be set and be a non-empty string."""
    assert hasattr(bunkr_api, "__version__")
    assert isinstance(bunkr_api.__version__, str)
    assert bunkr_api.__version__ != ""


def test_all_exports_are_resolvable():
    """Every name listed in __all__ must actually exist on the package,
    so `from bunkr_api import *` never silently drops or errors on a name.
    """
    assert hasattr(bunkr_api, "__all__")
    for name in bunkr_api.__all__:
        assert hasattr(bunkr_api, name), f"{name!r} is in __all__ but not defined"


def test_bunkr_api_instantiable_via_top_level_import(tmp_path):
    """Sanity check: the re-exported class is the real, usable class,
    not a stub or partial import.
    """
    from bunkr_api import BunkrAPI

    instance = BunkrAPI(db_path=str(tmp_path / "test.db"))

    assert instance.db is not None
    assert instance.scraper is not None
    assert instance.downloader is not None
    assert instance.player is not None