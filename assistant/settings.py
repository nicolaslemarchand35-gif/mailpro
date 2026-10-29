"""Reglages utilisateur (stockes dans Firestore `users/{uid}/settings/main`)."""
import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULTS = {
    'timezone': 'Europe/Paris',
    'work_days': [0, 1, 2, 3, 4],          # lundi=0 ... dimanche=6
    'work_start': '08:00',
    'work_end': '18:00',                    # exclu : 18:00 est hors plage
    'history_days': 7,                      # historique initial importe
    'retention_days': 90,                   # purge des resumes stockes
    'watched_folders': ['inbox'],           # 'inbox' ou identifiants EWS
    'priority_contacts': [],                # adresses ou domaines (@exemple.fr)
    'user_name': '',
    'signature': '',
    'writing_style': 'Vouvoiement, ton professionnel et cordial, phrases courtes.',
    'ai_enabled': True,
    'ai_model': '',                         # vide = OPENROUTER_MODEL (Render)
    'ai_on_bulk': False,                    # newsletters / notifications : pas d'IA
    'ai_daily_limit': 45,                   # garde-fou quota OpenRouter gratuit
    'auto_draft': False,                    # brouillons Exchange automatiques
    'auto_move': False,                     # deplacements automatiques (projets valides)
    'auto_move_min_confidence': 0.85,
    'a_savoir_expire_days': 10,
}

TIME_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')
MODEL_RE = re.compile(r'^[A-Za-z0-9._/:-]{0,150}$')


def merged(raw):
    out = dict(DEFAULTS)
    for key, value in (raw or {}).items():
        if key in DEFAULTS:
            out[key] = value
    return out


def validate(patch):
    """Valide un correctif partiel ; renvoie (propre, erreurs)."""
    clean, errors = {}, []
    for key, value in (patch or {}).items():
        if key not in DEFAULTS:
            errors.append(f'Reglage inconnu : {key}')
            continue
        default = DEFAULTS[key]
        if key == 'timezone':
            try:
                ZoneInfo(str(value))
            except (ZoneInfoNotFoundError, ValueError):
                errors.append('Fuseau horaire invalide')
                continue
        elif key in ('work_start', 'work_end'):
            if not isinstance(value, str) or not TIME_RE.match(value):
                errors.append(f'{key} : format HH:MM attendu')
                continue
        elif key == 'work_days':
            if not isinstance(value, list) or not all(isinstance(d, int) and 0 <= d <= 6 for d in value):
                errors.append('Jours ouvres invalides')
                continue
            value = sorted(set(value))
        elif isinstance(default, bool):
            if not isinstance(value, bool):
                errors.append(f'{key} : vrai/faux attendu')
                continue
        elif isinstance(default, int):
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 3650:
                errors.append(f'{key} : nombre entier attendu')
                continue
        elif isinstance(default, float):
            if not isinstance(value, (int, float)) or not 0 <= value <= 1:
                errors.append(f'{key} : valeur entre 0 et 1 attendue')
                continue
            value = float(value)
        elif isinstance(default, list):
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value) or len(value) > 200:
                errors.append(f'{key} : liste de textes attendue')
                continue
            value = [v.strip()[:500] for v in value if v.strip()]
            if key == 'priority_contacts':
                value = [v.lower() for v in value]
            if key == 'watched_folders' and not value:
                value = ['inbox']
        elif key == 'ai_model':
            if not isinstance(value, str) or not MODEL_RE.match(value.strip()):
                errors.append('Identifiant de modèle invalide')
                continue
            value = value.strip()
        elif isinstance(default, str):
            if not isinstance(value, str):
                errors.append(f'{key} : texte attendu')
                continue
            value = value[:2000]
        clean[key] = value
    return clean, errors


def in_window(now, cfg):
    """Vrai si `now` (aware) est dans la plage de travail configuree."""
    try:
        local = now.astimezone(ZoneInfo(cfg.get('timezone') or 'Europe/Paris'))
    except ZoneInfoNotFoundError:
        local = now.astimezone(ZoneInfo('Europe/Paris'))
    if local.weekday() not in cfg.get('work_days', DEFAULTS['work_days']):
        return False
    hm = local.strftime('%H:%M')
    return cfg.get('work_start', '08:00') <= hm < cfg.get('work_end', '18:00')


def local_today(cfg, now=None):
    tz = ZoneInfo(cfg.get('timezone') or 'Europe/Paris')
    return (now or datetime.now(tz)).astimezone(tz)
