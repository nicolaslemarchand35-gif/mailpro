"""Lecture centralisee de la configuration (variables d'environnement uniquement).

Aucun secret n'est ecrit sur disque ni renvoye par l'API. Les reglages
modifiables par l'utilisateur (horaires, projets...) vivent dans Firestore,
voir `settings.py`.
"""
import json
import os


def env(name, default=''):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None or value == '':
        return default
    return value.strip().lower() in ('1', 'true', 'yes', 'oui', 'on')


def env_int(name, default):
    try:
        return int(os.environ.get(name, ''))
    except ValueError:
        return default


def ews_endpoint():
    return env('EWS_ENDPOINT', 'https://ex.mail.ovh.net/EWS/Exchange.asmx')


def allowed_uids():
    return [u.strip() for u in env('ALLOWED_UIDS', env('ALLOWED_UID')).split(',') if u.strip()]


def owner_uid():
    """UID proprietaire des donnees (un seul utilisateur, une seule boite)."""
    uids = allowed_uids()
    return uids[0] if uids else 'owner'


def store_backend():
    if env('STORE_BACKEND'):
        return env('STORE_BACKEND')
    return 'firestore' if env_bool('FIRESTORE_ENABLED') else 'memory'


def firebase_web_config():
    """Configuration web Firebase (publique par nature : apiKey, projectId...)."""
    raw = env('FIREBASE_WEB_CONFIG')
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    keep = ('apiKey', 'authDomain', 'projectId')
    return {k: data[k] for k in keep if k in data} or None


def openrouter_key():
    """Cle OpenRouter : API_OPENROUTER (nom retenu sur Render), OPENROUTER_API_KEY accepte."""
    return env('API_OPENROUTER') or env('OPENROUTER_API_KEY')


def openrouter_models():
    raw = env('OPENROUTER_MODEL', 'nvidia/nemotron-3.5-lightning:free,openrouter/free')
    return [m.strip() for m in raw.split(',') if m.strip()]


def tick_budget_seconds():
    # cron-job.org coupe a 30 s : on garde une marge.
    return max(5, env_int('TICK_BUDGET_SECONDS', 24))
