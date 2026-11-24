"""Private API credential policy. Legacy entries contain only a token digest."""
import hashlib
import hmac


def authorize(clients, token, peer, path):
    supplied = hashlib.sha256(token.encode()).hexdigest()
    for owner, entry in clients.items():
        policy = entry if isinstance(entry, dict) else {}
        digest = policy.get("digest", entry)
        if not isinstance(digest, str) or not hmac.compare_digest(supplied, digest):
            continue
        scope = policy.get("scope", "both")
        if policy.get("peer") and policy["peer"] != peer:
            return None, None, "peer_denied"
        if scope not in ("both", "jobs", "sessions"):
            return None, None, "scope_denied"
        if scope != "both" and path != "/v1/capabilities" and path.split("/")[:3] != ["", "v1", scope]:
            return None, None, "scope_denied"
        return owner, scope, None
    return None, None, "unauthorized"
