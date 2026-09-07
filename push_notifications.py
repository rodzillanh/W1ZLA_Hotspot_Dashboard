"""Web Push notifications for the Pocket Dash mobile app
(mobile-dashboard-handoff.md, PR 3).

One self-contained client, same contract as every other integration
here: it NEVER raises out of its public methods. `send()` returns a
status string; `send_all()` returns counts plus the endpoints that
should be pruned. `generate_vapid_keys()` is the one thing that can
raise -- it runs once at first-run setup, where a failure should be
loud rather than silently leaving the feature half-configured.

`pywebpush` is an OPTIONAL import: an install that hasn't rebuilt since
this shipped won't have it yet, so the module still imports and
`PushClient.configured` is just False -- the feature is inert, not a
crash. `cryptography` (used only by generate_vapid_keys) is already a
dependency via paramiko, so key generation works even before pywebpush
lands.
"""
import base64
import json

try:
    from pywebpush import webpush, WebPushException
    _HAVE_PYWEBPUSH = True
except Exception:  # ImportError, or a broken transitive dependency
    _HAVE_PYWEBPUSH = False

    class WebPushException(Exception):  # placeholder so `except` below parses
        pass


def generate_vapid_keys():
    """Return (public_key_b64url, private_key_b64url) for a fresh P-256
    keypair, both as the raw base64url forms the ecosystem expects:

      - public: the 65-byte uncompressed EC point -- exactly what a
        browser passes as `applicationServerKey`.
      - private: the 32-byte private scalar -- what pywebpush accepts
        directly as `vapid_private_key` (a full PKCS8 PEM string does
        NOT work there: pywebpush hands a non-file string straight to
        py_vapid's from_string(), which wants the raw scalar).
    """
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization

    priv = ec.generate_private_key(ec.SECP256R1())
    pub_point = priv.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    priv_scalar = priv.private_numbers().private_value.to_bytes(32, "big")

    def _b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

    return _b64(pub_point), _b64(priv_scalar)


class PushClient:
    """Wraps pywebpush. Rebuilt from settings on save, same
    rebuild-on-settings-change pattern as the other integration clients
    in app.py."""

    def __init__(self, public_key: str = "", private_key: str = "",
                 contact: str = "mailto:admin@example.com"):
        self._public_key = (public_key or "").strip()
        self._private_key = (private_key or "").strip()
        self._contact = (contact or "").strip() or "mailto:admin@example.com"

    @property
    def configured(self) -> bool:
        return bool(_HAVE_PYWEBPUSH and self._public_key and self._private_key)

    @property
    def public_key(self) -> str:
        return self._public_key

    def send(self, subscription: dict, payload: dict) -> str:
        """Send one notification. Returns:
          "ok"      -- accepted by the push service
          "expired" -- 404/410: the subscription is dead, prune it
          "failed"  -- anything else (transient, misconfigured, bad input)
        Never raises.
        """
        if not self.configured:
            return "failed"
        try:
            webpush(
                subscription_info=subscription,
                data=json.dumps(payload),
                vapid_private_key=self._private_key,
                vapid_claims={"sub": self._contact},
                timeout=10,
            )
            return "ok"
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):
                return "expired"
            print(f"[push] send failed (HTTP {code}): {e}")
            return "failed"
        except Exception as e:  # network error, malformed subscription, etc.
            print(f"[push] send error: {e}")
            return "failed"

    def send_all(self, subscriptions: list, payload: dict):
        """Send `payload` to every subscription.

        Returns (sent, failed, expired_endpoints). The caller is
        responsible for pruning `expired_endpoints` from storage --
        this client is stateless and doesn't touch the subscription file.
        """
        sent = failed = 0
        expired = []
        for sub in subscriptions:
            result = self.send(sub, payload)
            if result == "ok":
                sent += 1
            elif result == "expired":
                ep = sub.get("endpoint")
                if ep:
                    expired.append(ep)
            else:
                failed += 1
        return sent, failed, expired
