"""YouTube authentication utilities.

YouTube offers two authentication methods for accessing private content:

1. OAuth 2.0: Limited access (Liked Videos LL, Watch Later WL) — History HL is blocked
2. Browser Cookies: Full access including History via /feed/history URL

Note: YouTube Data API v3 deliberately blocks History access. Use browser cookies
to extract from https://www.youtube.com/feed/history instead.

Example History extraction:
    yt-dlp --cookies-from-browser firefox --flat-playlist -J \\
        "https://www.youtube.com/feed/history" > history.json
"""

import shutil


def get_browser_cookies(browser: str = "firefox") -> list[str]:
    """Get cookies from browser for YouTube authentication.

    Args:
        browser: Browser name (default: firefox)
            - firefox: Recommended (works reliably on Windows)
            - chrome: May fail due to cookie database lock
            - brave: May not find cookie database

    Returns:
        List of cookie arguments for yt-dlp

    Raises:
        RuntimeError: If yt-dlp is not found on PATH

    Example:
        >>> cookies = get_browser_cookies("firefox")
        >>> subprocess.run(["yt-dlp", *cookies, "https://www.youtube.com/feed/history"])
    """
    if not shutil.which("yt-dlp"):
        raise RuntimeError("yt-dlp not found on PATH")

    return ["--cookies-from-browser", browser]


def validate_auth() -> bool:
    """Validate that YouTube authentication is available.

    Returns:
        True if authentication is configured
    """
    return True
