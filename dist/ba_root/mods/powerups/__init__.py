"""Powerup Manager package for BombSquad / Ballistica API 9."""


def install():
    """Install the runtime patches without editing Ballistica source files."""
    from .manager import install as _install

    return _install()
