"""Errors raised by the portal client. The API layer turns each one into an HTTP response."""


class PortalError(Exception):
    """Anything that went wrong while talking to the portal."""


class PortalAuthError(PortalError):
    """We could not log in, or the portal keeps rejecting our session or signature."""


class PortalUnavailable(PortalError):
    """The portal could not be reached, timed out, or answered with a 5xx."""


class PortalProtocolError(PortalError):
    """The portal answered, but not in the way PROTOCOL.md says it should."""


class PortalNotFound(PortalError):
    """The portal answered 404 (for example an unknown meter id)."""
