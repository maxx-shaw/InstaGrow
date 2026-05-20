"""
Platform dispatcher — routes all calls to the currently active platform module.
Routers continue to `import instagram_client as ig` unchanged.
"""
import importlib

_PLATFORMS = ("instagram", "linkedin", "x")
_active = "instagram"


def set_platform(platform: str):
    global _active
    if platform not in _PLATFORMS:
        raise ValueError(f"Unknown platform '{platform}'. Choose from: {_PLATFORMS}")
    _active = platform


def get_platform() -> str:
    return _active


def _mod():
    return importlib.import_module(f"platforms.{_active}")


def __getattr__(name: str):
    """Proxy every unresolved attribute to the active platform module."""
    return getattr(_mod(), name)


# Compat wrappers — scheduler passes target_username as user_id in v2
def follow_user(user_id: str, delay_min: int = 45, delay_max: int = 180) -> bool:
    return _mod().follow_user_by_username(user_id, delay_min=delay_min, delay_max=delay_max)


def unfollow_user(user_id: str, delay_min: int = 30, delay_max: int = 90) -> bool:
    return _mod().unfollow_user_by_username(user_id, delay_min=delay_min, delay_max=delay_max)
