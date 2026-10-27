"""
Common utility functions used across the application
"""
import re


def sanitize_name(name):
    """
    Sanitize name for use in VM names and identifiers.

    Removes all characters except lowercase letters, numbers, and hyphens.

    Args:
        name: String to sanitize

    Returns:
        Sanitized string containing only [a-z0-9-]

    Examples:
        >>> sanitize_name("John Doe")
        'johndoe'
        >>> sanitize_name("Web_Server-01")
        'webserver-01'
    """
    return re.sub(r'[^a-z0-9-]', '', name.lower())
