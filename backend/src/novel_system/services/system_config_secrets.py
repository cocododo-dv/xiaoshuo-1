"""系统配置里的密钥：服务的 API key 用 NOVEL_SYSTEM_CONFIG_SECRET 派生的 Fernet 密钥加密存库（从 system_config 拆出，B09-13）。

密钥 id：``llm_provider:<provider_id>:api_key``；最早的单服务配置用过 ``llm_api_key``，读的时候仍作为
openai / openai_compatible 两个服务的兜底。任何对外载荷只带状态（有没有、能不能解开、提示），不带明文。
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from novel_system.db.models import SystemSecret
from novel_system.env_config import env_config_secret
from novel_system.services.errors import DomainError


LLM_API_KEY_SECRET_ID = "llm_api_key"
LLM_PROVIDER_SECRET_PREFIX = "llm_provider"


def llm_provider_api_key_secret_id(provider_id: str) -> str:
    return f"{LLM_PROVIDER_SECRET_PREFIX}:{provider_id}:api_key"


def secret_value(session: Session, secret_id: str) -> str | None:
    """解密后的密钥；没有这一条、没配 NOVEL_SYSTEM_CONFIG_SECRET 或解不开 → ``None``。"""
    secret = session.get(SystemSecret, secret_id)
    if secret is None:
        return None
    try:
        return decrypt_secret(secret.encrypted_value)
    except (DomainError, InvalidToken):
        return None


def secret_status(session: Session, secret_id: str, *, secret: SystemSecret | None = None) -> dict[str, Any]:
    """密钥的三态：有没有这一条、能不能用当前的 NOVEL_SYSTEM_CONFIG_SECRET 解开、提示与元数据（不含明文）。"""
    item = secret or session.get(SystemSecret, secret_id)
    decryptable = False
    if item is not None and item.encrypted_value:
        # BUG-001: secret 记录"存在"不等于"可解密"。config.secret 轮换后旧密文
        # InvalidToken,运行期 load_secret_value 静默返 None → api_key 为空,但
        # 就绪侧若只看 configured 就会假阳性。这里真正试解密,把三态拆开。
        try:
            decrypted = decrypt_secret(item.encrypted_value)
            decryptable = bool(decrypted and decrypted.strip())
        except (DomainError, InvalidToken):
            decryptable = False
    return {
        "configured": item is not None,
        "decryptable": decryptable,
        "hint": item.value_hint if item is not None else None,
        "secret_type": item.secret_type if item is not None else None,
        "metadata": item.metadata_json if item is not None else {},
        "expires_at": item.expires_at if item is not None else None,
        "updated_at": item.updated_at if item is not None else None,
    }


def none_secret_status() -> dict[str, Any]:
    return {
        "configured": False,
        "decryptable": False,
        "hint": None,
        "secret_type": "none",
        "metadata": {},
        "expires_at": None,
        "updated_at": None,
    }


def save_secret_value(
    session: Session,
    *,
    secret_id: str,
    raw_value: str,
    actor_ref: str,
    secret_type: str,
    metadata: dict[str, Any],
    expires_at: str | None = None,
) -> dict[str, Any]:
    secret = session.get(SystemSecret, secret_id)
    encrypted = encrypt_secret(raw_value)
    if secret is None:
        secret = SystemSecret(
            secret_id=secret_id,
            encrypted_value=encrypted,
            value_hint=mask_secret(raw_value),
            secret_type=secret_type,
            metadata_json=metadata,
            expires_at=expires_at,
            updated_by=actor_ref,
        )
    else:
        secret.encrypted_value = encrypted
        secret.value_hint = mask_secret(raw_value)
        secret.secret_type = secret_type
        secret.metadata_json = metadata
        secret.expires_at = expires_at
        secret.updated_by = actor_ref
    session.add(secret)
    return secret_status(session, secret_id, secret=secret)


def encrypt_secret(value: str) -> str:
    return fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(value: str) -> str:
    return fernet().decrypt(value.encode("utf-8")).decode("utf-8")


def fernet() -> Fernet:
    secret = env_config_secret()
    if not secret:
        raise DomainError("CONFIG_SECRET_REQUIRED", "NOVEL_SYSTEM_CONFIG_SECRET is required to manage secrets", 403)
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def mask_secret(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}...{value[-4:]}"
