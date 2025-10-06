import hashlib
import base64

def generate_reflection_id(email: str) -> str:
    """
    Generates a lawful reflection ID (seal) from a user's email.
    Hashes securely with SHA-256 and encodes to base64.
    """
    if not email:
        raise ValueError("Email required for lawful reflection ID")

    # Step 1: Hash the email securely
    hashed_bytes = hashlib.sha256(email.lower().encode("utf-8")).digest()

    # Step 2: Encode to base64 for compact ID
    reflection_id = base64.urlsafe_b64encode(hashed_bytes).decode("utf-8").rstrip("=")

    # Step 3: Add glyph prefix (for symbolic identification)
    return f"GLYPH-{reflection_id[:16]}"
