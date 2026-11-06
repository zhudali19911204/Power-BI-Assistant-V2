"""Add a model without putting its API key in source, shell arguments, or reports."""
import argparse
import getpass
import json
import sys

from backend.app import create_app
from backend.schemas import LLMProfileInput


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--name', default='')
    parser.add_argument('--model', required=True)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--key-stdin', action='store_true')
    parser.add_argument('--activate', action='store_true')
    args = parser.parse_args()
    if args.key_stdin:
        print('WAITING_FOR_API_KEY', flush=True)
        key = sys.stdin.readline().strip()
    else:
        key = getpass.getpass('API Key (hidden): ')
    if not key:
        raise SystemExit('API Key is required')
    service = create_app().state.settings
    # Re-running the same explicit import updates that profile instead of creating duplicates.
    existing = next((p for p in service.profiles() if p['provider'] == 'openai_compat'
                     and p['model'] == args.model and p['base_url'] == args.base_url.rstrip('/')), None)
    profile = service.save_profile(LLMProfileInput(name=args.name, model=args.model,
        base_url=args.base_url, api_key=key, enable_thinking=None), existing['id'] if existing else None)
    del key
    if args.activate:
        service.activate(profile['id'])
    print(json.dumps({'saved_encrypted': True, 'model': profile['model'], 'id': profile['id'],
                      'profiles': len(service.profiles()), 'active': service.public()['model']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
