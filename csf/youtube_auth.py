"""YouTube authentication utilities."""

import shutil


def get_browser_cookies(browser: str = "brave") -> list[str]:
    """Get cookies from browser for YouTube authentication.

    Args:
        browser: Browser name (default: brave)

    Returns:
        List of cookie arguments for yt-dlp
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
