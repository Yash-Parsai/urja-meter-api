"""Request signing for the portal's bulk export. The recipe is explained in PROTOCOL.md, section 4."""
import hashlib
import hmac


def sign(secret: str, method: str, path: str, query: str, timestamp: int) -> str:
    """Return the value for the X-Signature header.

    HMAC-SHA256, keyed with the signing secret, over four lines:
        METHOD
        PATH   (no query string)
        QUERY  (no leading '?')
        TIMESTAMP  (unix seconds, the same number sent in X-Timestamp)
    """
    message = "\n".join([method.upper(), path, query, str(timestamp)])
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
