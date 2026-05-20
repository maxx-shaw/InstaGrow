"""
v2 adapter — delegates to the Playwright-based Instagram platform.
All existing routers import from this module unchanged.
"""
from platforms.instagram import (
    login_state,
    is_logged_in,
    get_my_user_id,
    get_session_json,
    login_async,
    login_by_sessionid_async,
    submit_challenge_code,
    submit_totp_code,
    follow_user_by_username,
    unfollow_user_by_username,
    get_following,
    get_followers,
    get_user_info,
    get_user_info_by_username,
    logout,
    human_delay,
    action_delay,
)

# Compat wrappers — scheduler passes target_username as user_id in v2
def follow_user(user_id: str, delay_min: int = 45, delay_max: int = 180) -> bool:
    return follow_user_by_username(user_id, delay_min=delay_min, delay_max=delay_max)


def unfollow_user(user_id: str, delay_min: int = 30, delay_max: int = 90) -> bool:
    return unfollow_user_by_username(user_id, delay_min=delay_min, delay_max=delay_max)
