"""Persist preferences in SQLite and encrypt credentials with a local Fernet key."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException

from .database import Database, encode, now, uid
from .engine.llm.claude import ClaudeProvider
from .engine.llm.openai_compat import OpenAICompatProvider
from .engine.net import enable_os_trust_store
from .schemas import LLMProfileInput, SettingsInput


class SettingsService:
    def __init__(self, db: Database, directory: Path):
        self.db = db
        key_path = directory / "secret.key"
        try:
            with key_path.open("xb") as handle:
                handle.write(Fernet.generate_key())
            os.chmod(key_path, 0o600)
        except FileExistsError:
            pass
        self.cipher = Fernet(key_path.read_bytes())
        self._migrate()

    @staticmethod
    def defaults():
        return {
            "provider": os.getenv("PBI_LLM_PROVIDER", "openai_compat"),
            "model": os.getenv("PBI_LLM_MODEL", ""),
            "base_url": os.getenv("PBI_LLM_BASE_URL", ""),
            "encrypted_key": "",
            "enable_thinking": None,
            "use_env_key": True,
        }

    def raw(self):
        row = self.db.one("SELECT value FROM settings WHERE id=1")
        return {**self.defaults(), **(json.loads(row["value"]) if row else {})}

    @staticmethod
    def _select(db, profile_id, cfg):
        db.execute("INSERT OR REPLACE INTO settings VALUES (1,?)",
                   (encode({**cfg, "active_profile_id": profile_id}),))

    def _migrate(self):
        """Import the previous single configuration once, without changing its credentials."""
        with self.db.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM settings WHERE id=1").fetchone()
            cfg = {**self.defaults(), **(json.loads(row["value"]) if row else {})}
            if cfg.get('active_profile_id') and db.execute(
                    'SELECT id FROM llm_profiles WHERE id=?', (cfg['active_profile_id'],)).fetchone():
                return
            existing = db.execute('SELECT * FROM llm_profiles ORDER BY created_at,id LIMIT 1').fetchone()
            if existing:
                self._select(db, existing['id'], json.loads(existing['config']))
                return
            if not any(cfg.get(k) for k in ('model', 'base_url', 'encrypted_key')):
                return
            cfg.pop('active_profile_id', None)
            # Capture an environment credential so the imported profile remains independent.
            if not cfg.get('encrypted_key') and cfg.get('use_env_key', True):
                env_key = os.getenv('PBI_LLM_API_KEY', '')
                if env_key:
                    cfg['encrypted_key'] = self.cipher.encrypt(env_key.encode()).decode()
            cfg['use_env_key'] = False
            profile_id, stamp = uid(), now()
            db.execute('INSERT INTO llm_profiles VALUES (?,?,?,?,?)', (
                profile_id, cfg['model'] or '原有模型配置', encode(cfg), stamp, stamp))
            self._select(db, profile_id, cfg)

    def key(self, cfg):
        if cfg.get("encrypted_key"):
            try:
                return self.cipher.decrypt(cfg["encrypted_key"].encode()).decode()
            except InvalidToken:
                raise HTTPException(409, "无法解密 API Key，请重新配置；迁移时需同时保留 secret.key")
        return os.getenv("PBI_LLM_API_KEY", "") if cfg.get('use_env_key', True) else ''

    def _public_config(self, cfg):
        key = self.key(cfg)
        return {k: cfg[k] for k in ("provider", "model", "base_url", "enable_thinking")} | {
            "has_key": bool(key), "ready": bool(cfg["model"] and key and
                (cfg["base_url"] or cfg["provider"] == "claude")),
        }

    def public(self):
        cfg = self.raw()
        profile_id = cfg.get('active_profile_id')
        row = self.db.one('SELECT name FROM llm_profiles WHERE id=?', (profile_id,)) if profile_id else None
        return self._public_config(cfg) | {
            'active_profile_id': profile_id, 'name': row['name'] if row else '',
        }

    def profiles(self):
        active_id = self.raw().get('active_profile_id')
        return [self._public_config(json.loads(row['config'])) | {
            'id': row['id'], 'name': row['name'], 'active': row['id'] == active_id,
        } for row in self.db.all('SELECT * FROM llm_profiles ORDER BY created_at,id')]

    def _profile(self, profile_id):
        row = self.db.one('SELECT * FROM llm_profiles WHERE id=?', (profile_id,))
        if not row:
            raise HTTPException(404, '模型配置不存在，请刷新模型列表')
        return row

    @staticmethod
    def validate_url(url):
        if not url:
            return
        parts = urlsplit(url)
        if (parts.scheme not in ("http", "https") or not parts.hostname
                or parts.username or parts.password or parts.query or parts.fragment):
            raise HTTPException(422, "API 地址必须为 http(s) 地址，且不包含凭据、查询参数或锚点")
        # Private LAN inference services may use HTTP.

    def save(self, body: SettingsInput):
        """Keep the original settings API working by editing the selected profile."""
        self._save_profile(body, profile_id=self.raw().get('active_profile_id'), select=True)
        return self.public()

    def save_profile(self, body: LLMProfileInput, profile_id=None):
        if not body.model:
            raise HTTPException(422, '请填写模型名称')
        return self._save_profile(body, profile_id=profile_id)

    def _save_profile(self, body, profile_id=None, *, select=False):
        self.validate_url(body.base_url)
        with self.db.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM llm_profiles WHERE id=?', (profile_id,)).fetchone() if profile_id else None
            if profile_id and not row:
                raise HTTPException(404, '模型配置不存在，请刷新模型列表')
            cfg = json.loads(row['config']) if row else {
                'encrypted_key': '', 'enable_thinking': None, 'use_env_key': False,
            }
            # Blank means retain only this profile's key. Never borrow from another profile.
            if body.clear_key:
                cfg['encrypted_key'] = ''
            elif body.api_key:
                cfg['encrypted_key'] = self.cipher.encrypt(body.api_key.encode()).decode()
            cfg['use_env_key'] = False
            cfg.update(provider=body.provider, model=body.model, base_url=body.base_url.rstrip('/'))
            if 'enable_thinking' in body.model_fields_set:
                cfg['enable_thinking'] = body.enable_thinking
            name = (body.name or body.model) if isinstance(body, LLMProfileInput) else (row['name'] if row else body.model)
            name = name or '未命名模型'
            profile_id, stamp = profile_id or uid(), now()
            if row:
                db.execute('UPDATE llm_profiles SET name=?,config=?,updated_at=? WHERE id=?',
                           (name, encode(cfg), stamp, profile_id))
            else:
                db.execute('INSERT INTO llm_profiles VALUES (?,?,?,?,?)',
                           (profile_id, name, encode(cfg), stamp, stamp))
            saved = db.execute('SELECT value FROM settings WHERE id=1').fetchone()
            selected_id = json.loads(saved['value']).get('active_profile_id') if saved else None
            if select or selected_id == profile_id or not selected_id:
                self._select(db, profile_id, cfg)
        return self._public_config(cfg) | {
            'id': profile_id, 'name': name,
            'active': self.raw().get('active_profile_id') == profile_id,
        }

    def activate(self, profile_id):
        with self.db.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM llm_profiles WHERE id=?', (profile_id,)).fetchone()
            if not row:
                raise HTTPException(404, '模型配置不存在，请刷新模型列表')
            self._select(db, profile_id, json.loads(row['config']))
        return self.public()

    def delete_profile(self, profile_id):
        with self.db.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT value FROM settings WHERE id=1').fetchone()
            selected_id = json.loads(row['value']).get('active_profile_id') if row else None
            if not db.execute('DELETE FROM llm_profiles WHERE id=?', (profile_id,)).rowcount:
                raise HTTPException(404, '模型配置不存在，请刷新模型列表')
            if selected_id == profile_id:
                remaining = db.execute('SELECT * FROM llm_profiles ORDER BY created_at,id LIMIT 1').fetchone()
                cfg = json.loads(remaining['config']) if remaining else {
                    'provider': 'openai_compat', 'model': '', 'base_url': '',
                    'encrypted_key': '', 'enable_thinking': None, 'use_env_key': False,
                }
                self._select(db, remaining['id'] if remaining else None, cfg)
        return self.public()

    def provider(self, profile_id=None):
        cfg = json.loads(self._profile(profile_id)['config']) if profile_id else self.raw()
        key = self.key(cfg)
        if not key or not cfg["model"]:
            raise HTTPException(409, "请先在设置中配置模型名称和 API Key；本地模型可填入 local 作为 Key")
        if cfg["provider"] == "openai_compat" and not cfg["base_url"]:
            raise HTTPException(409, "请填写服务器提供的兼容 API 根地址，例如 http://服务器IP:8000/v1")
        enable_os_trust_store()
        if cfg["provider"] == "claude":
            provider = ClaudeProvider(key, cfg["model"])
            if cfg["base_url"]:
                from anthropic import Anthropic
                provider._client = Anthropic(api_key=key, base_url=cfg["base_url"],
                                             timeout=120, max_retries=1)
        else:
            provider = OpenAICompatProvider(key, cfg["model"], cfg["base_url"],
                                             enable_thinking=cfg['enable_thinking'])
        provider._client = provider._client.with_options(timeout=120, max_retries=1)
        return provider
