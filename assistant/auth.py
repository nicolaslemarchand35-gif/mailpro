"""Authentification de l'interface.

- Production : jeton d'identite Firebase Authentication verifie cote serveur
  (signature, expiration, projet) PUIS controle de l'UID dans ALLOWED_UIDS.
  Un compte Firebase valide mais non autorise est refuse.
- Option `UI_TOKEN_LOGIN=true` (essais locaux) : connexion avec ADMIN_API_TOKEN.
"""
import hmac

from . import config


class AuthError(Exception):
    def __init__(self, status, code):
        super().__init__(code)
        self.status = status
        self.code = code


def bearer(header):
    if not header or not header.startswith('Bearer '):
        return None
    token = header[7:].strip()
    return token or None


def token_matches(token, secret):
    return bool(secret and len(secret) >= 32 and token and
                hmac.compare_digest(token.encode('utf-8'), secret.encode('utf-8')))


def verify_firebase(token):
    from firebase_admin import auth as fb_auth
    from .store import firebase_app
    try:
        return fb_auth.verify_id_token(token, app=firebase_app(), clock_skew_seconds=10)
    except Exception:
        raise AuthError(401, 'invalid_token')


def authenticate(header, verifier=None):
    """Renvoie {'uid', 'email', 'method'} ou leve AuthError."""
    token = bearer(header)
    if not token:
        raise AuthError(401, 'unauthorized')
    allowed = config.allowed_uids()
    if config.env_bool('UI_TOKEN_LOGIN') and token_matches(token, config.env('ADMIN_API_TOKEN')):
        return {'uid': config.owner_uid(), 'email': None, 'method': 'token'}
    if not config.firebase_web_config():
        raise AuthError(401, 'unauthorized')
    claims = (verifier or verify_firebase)(token)
    uid = claims.get('uid') or claims.get('sub')
    if not uid or uid not in allowed:
        # Le message inclut l'UID de l'appelant pour aider a configurer ALLOWED_UIDS.
        raise AuthError(403, 'forbidden_uid:' + str(uid)[:128])
    return {'uid': uid, 'email': claims.get('email'), 'method': 'firebase'}
