import hmac
import hashlib
import time


def verify_linq_signature(timestamp: str, body: bytes, signature: str, secret: str) -> bool:
    """Verify Linq webhook HMAC-SHA256 signature."""
    # Reject old timestamps (> 5 min)
    try:
        ts = int(timestamp)
        if abs(time.time() - ts) > 300:
            return False
    except (ValueError, TypeError):
        return False

    expected = hmac.new(
        secret.encode(),
        f"{timestamp}.{body.decode()}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
