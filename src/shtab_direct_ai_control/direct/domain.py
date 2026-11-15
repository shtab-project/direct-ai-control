import hashlib
import json
import re

INITIAL = ['вакансии', 'работа', 'резюме', 'обучение', 'курсы', 'реферат', 'диплом', 'картинки', 'фото', 'видео', 'форум', 'авито', 'бу', 'игрушка', 'рефераты']
TEST_PHRASES = ['своими руками', 'бесплатно', 'скачать схему']

def checksum(items: list[str]) -> str:
    return hashlib.sha256(json.dumps(sorted(items), ensure_ascii=False).encode()).hexdigest()

def diff(before: list[str], after: list[str]) -> dict:
    return {'add': [x for x in after if x not in before], 'keep': [x for x in before if x in after], 'remove': [x for x in before if x not in after]}

def validate(items: list[str]) -> dict:
    errors, warnings, seen = [], [], set()
    for raw in items:
        p = raw.strip()
        if not p:
            errors.append('Пустая минус-фраза.')
        if p in seen:
            errors.append(f'Точный дубль: «{p}».')
        seen.add(p)
        if re.search(r'[^\w\s!+\[\]"()|\-]', p, re.UNICODE):
            errors.append(f'Недопустимые символы: «{p}».')
        for left, right in [('[', ']'), ('(', ')')]:
            depth = 0
            for char in p:
                depth += (char == left) - (char == right)
                if depth < 0:
                    break
            if depth != 0 or p.count(left) != p.count(right):
                errors.append(f'Непарные операторы: «{p}».')
        if p.count('"') % 2:
            errors.append(f'Непарные кавычки: «{p}».')
        if len(p) > 1000 or len(p.split()) > 7:
            errors.append(f'Превышен консервативный лимит DEMO (7 слов / 1000 символов): «{p}».')
        if any(stem in p.lower() for stem in ['ремонт', 'свароч', 'сервис', 'станок', 'чпу', 'фитнес', 'подъем', 'подъём']):
            warnings.append(f'«{p}» может пересекаться с целевыми запросами. Требуется ручная проверка; морфологический анализ ещё не подключён.')
    if len(items) > 1000:
        errors.append('Превышен лимит DEMO: 1000 фраз.')
    return {'errors': list(dict.fromkeys(errors)), 'warnings': list(dict.fromkeys(warnings)), 'valid': not errors}

def metrics(rows: list[dict]) -> dict:
    result = {k: sum(r[k] for r in rows) for k in ['cost', 'impressions', 'clicks', 'conversions']}
    result.update(ctr=result['clicks'] / result['impressions'] * 100 if result['impressions'] else 0,
                  cpc=result['cost'] / result['clicks'] if result['clicks'] else 0,
                  cpa=result['cost'] / result['conversions'] if result['conversions'] else 0)
    return result
