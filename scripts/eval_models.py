"""Compare des modeles OpenRouter sur les mails FICTIFS du mode demo (aucune donnee reelle).

    OPENROUTER_API_KEY=... python -m scripts.eval_models google/gemma-4-31b-it:free openrouter/free
"""
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

from assistant import ai
from assistant.settings import DEFAULTS
from scripts.demo import SAMPLES

EXPECTED = ['a_traiter', 'a_savoir', 'a_traiter', 'ignorer', 'a_traiter', 'a_savoir']


def context():
    return {'settings': {**DEFAULTS, 'user_name': 'Nicolas'}, 'today': date.today(), 'thread': [], 'replied_at': None,
            'projects': [('ptilleuls', {'name': 'Résidence Les Tilleuls', 'keywords': ['tilleuls'], 'contacts': []}),
                         ('pferry', {'name': 'Ecole Jules Ferry', 'keywords': ['jules ferry'], 'contacts': []})],
            'folder_choices': [('f1', 'Boîte de réception / Chantiers / Résidence Les Tilleuls'),
                               ('f2', 'Boîte de réception / Chantiers / Ecole Jules Ferry'),
                               ('f3', 'Boîte de réception / Newsletters')]}


def main(models):
    now = datetime.now(timezone.utc)
    for model in models:
        os.environ['OPENROUTER_MODEL'] = model
        good, durations, errors, leaks = 0, [], [], 0
        for (subject, sender, body, hours), expected in zip(SAMPLES, EXPECTED):
            item = {'subject': subject, 'from': {'name': '', 'email': sender}, 'to': [], 'cc': [],
                    'received_at': now - timedelta(hours=hours), 'body': body, 'unique_body': None,
                    'attachments': [], 'headers': {}}
            started = time.monotonic()
            try:
                out = ai.analyze_with_ai(item, context(), timeout=40)
            except ai.AiError as exc:
                errors.append(exc.code)
                continue
            durations.append(time.monotonic() - started)
            good += out['categorie'] == expected
            leaks += 'pirate@' in (out.get('brouillon') or '')
            print(f"  [{out['modele']}] {subject[:40]:40} -> {out['categorie']:9} ({expected}) "
                  f"echeance={out['echeance']}/{out['echeance_source']}")
        avg = sum(durations) / len(durations) if durations else 0
        print(f'{model}: {good}/{len(SAMPLES)} categories justes, {avg:.1f} s/mail, erreurs={errors}, '
              f'injection suivie={leaks}\n')


if __name__ == '__main__':
    main(sys.argv[1:] or ['openrouter/free'])
