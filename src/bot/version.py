from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("bluesky-repost-bot")
except PackageNotFoundError:
    __version__ = "dev"
