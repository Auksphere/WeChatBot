"""Account-scoped files shared by the bot and the local manager."""
import json
import re
import uuid
from pathlib import Path

MAX_PACKS = 6
MAX_ITEMS = 120


def read_json(path, default=None):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else (default or {})
    except (OSError, ValueError):
        return default or {}


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def valid_pack_id(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and not any(c.isspace() or ord(c) < 32 for c in value)


def valid_item(item):
    if not isinstance(item, dict):
        return False
    return (type(item.get('index')) is int
            and re.fullmatch(r'[0-9a-f]{32}', str(item.get('id', ''))) is not None
            and re.fullmatch(r'[0-9a-f]{64}', str(item.get('sha256', ''))) is not None
            and isinstance(item.get('description'), str) and 0 < len(item['description']) <= 160
            and (not item.get('pack_id') or valid_pack_id(item['pack_id'])))


def merged_policy(directory, account):
    directory = Path(directory)
    base = read_json(directory / 'auto-stickers.json')
    if base.get('account_id') != account:
        return {}
    base['stickers'] = [i for i in base.get('stickers', []) if valid_item(i)]
    library = read_json(directory / 'sticker-discovery-state.json')
    if library.get('account_id') == account:
        seen = {i['id'] for i in base['stickers']}
        indexes = {i['index'] for i in base['stickers']}
        for item in library.get('items', [])[:MAX_ITEMS]:
            if valid_item(item) and item['id'] not in seen and item['index'] not in indexes:
                base['stickers'].append(item)
                seen.add(item['id'])
                indexes.add(item['index'])
    return base


def discovery_status(directory, account):
    directory = Path(directory)
    settings = read_json(directory / 'sticker-discovery-settings.json')
    state = read_json(directory / 'sticker-discovery-state.json')
    if state.get('account_id') != account:
        state = {}
    return {'enabled': settings.get('account_id') == account and settings.get('enabled') is True,
            'phase': state.get('phase', 'idle'), 'last_run': state.get('last_run', 0),
            'last_success': state.get('last_success', 0), 'error': state.get('error', ''),
            'pack_count': sum(p.get('added') is True and not p.get('removed') for p in state.get('packs', {}).values()),
            'ready_packs': sum(p.get('status') == 'ready' and not p.get('removed') for p in state.get('packs', {}).values()),
            'failed_packs': sum(p.get('status') == 'failed' and not p.get('removed') for p in state.get('packs', {}).values()),
            'item_count': len(state.get('items', [])), 'max_packs': MAX_PACKS, 'max_items': MAX_ITEMS}
