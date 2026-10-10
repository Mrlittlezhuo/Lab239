"""用户/认证离线测试（任务 002）。

覆盖密码 scrypt、service 业务、FastAPI auth/admin 路由和首个管理员
CLI。get_db 依赖覆盖指向临时数据库，不连接模型、网络或真实
用户数据库。
"""
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import jwt as _pyjwt
import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from chatchat.server.auth import jwt as auth_jwt
from chatchat.server.auth import password as pw
from chatchat.server.auth import service
from chatchat.server.auth.schemas import LoginRequest
from chatchat.server.db.repository import user_repository as user_repo
from chatchat.server.db import session as db_session
from chatchat.server.db.migrate.base import apply_migrations
from chatchat.server.db.migrate.migrations import build_registry


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def secret(monkeypatch):
    """设置一个 >=32 字节的测试密钥（非真实密钥）。"""
    key = "unit-test-secret-key" + "0" * 20
    monkeypatch.setenv("CHATCHAT_AUTH_SECRET", key)
    return key


@pytest.fixture
def temp_user_db(tmp_path):
    """临时文件库，已升级到 v2（含 user_account）。"""
    path = tmp_path / "auth.db"
    path.touch()
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        apply_migrations(build_registry(), conn)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    yield {"engine": engine, "Session": Session, "path": path}
    engine.dispose()


@pytest.fixture
def client(temp_user_db, secret):
    """FastAPI TestClient，get_db 覆盖指向临时库。"""
    from chatchat.server.auth.routes_admin import admin_router
    from chatchat.server.auth.routes_auth import auth_router

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(admin_router)

    def override_get_db():
        s = temp_user_db["Session"]()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[db_session.get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client


def _mk_user(
    temp_user_db,
    username,
    role,
    password="password1234",
    must_change_password=False,
):
    """在临时库创建用户并返回其非敏感字段。"""
    S = temp_user_db["Session"]
    s = S()
    try:
        u = service.create_user(
            s, username=username, display_name=username.title(),
            password=password, role=role,  # 明文，create_user 内部加盐哈希
        )
        u.must_change_password = must_change_password
        s.commit()
        return {
            "id": u.id, "username": u.username, "role": u.role,
            "status": u.status, "auth_version": u.auth_version,
        }
    finally:
        s.close()


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _encode_token(payload, secret):
    return _pyjwt.encode(payload, secret, algorithm=auth_jwt.ALGORITHM)


def _admin_headers(temp_user_db, secret):
    adm = _mk_user(temp_user_db, "boss", "admin")
    return _bearer(auth_jwt.create_token(adm["id"], adm["auth_version"]))


def _user_headers(temp_user_db, secret, username="plain"):
    u = _mk_user(temp_user_db, username, "user")
    return _bearer(auth_jwt.create_token(u["id"], u["auth_version"]))


# ---------------------------------------------------------------------------
# 密码 scrypt
# ---------------------------------------------------------------------------


def test_hash_roundtrip_and_uniqueness():
    h1 = pw.hash_password("password1234")
    h2 = pw.hash_password("password1234")
    assert h1 != h2  # 独立盐
    assert pw.verify_password("password1234", h1)
    assert pw.verify_password("password1234", h2)
    assert not pw.verify_password("wrongpass999", h1)
    assert "password1234" not in h1  # 不含明文


def test_hash_format_and_params():
    h = pw.hash_password("password1234")
    parts = h.split("$")
    assert parts[0] == "scrypt"
    assert parts[1] == "1"  # 格式版本
    assert parts[2] == "n=16384,r=8,p=5"
    assert len(parts) == 5
    assert pw.SCRYPT_N == 16384 and pw.SCRYPT_R == 8 and pw.SCRYPT_P == 5
    assert pw.SCRYPT_DKLEN == 64


def test_unicode_and_space_password():
    h = pw.hash_password("päss word 中文字符")
    assert pw.verify_password("päss word 中文字符", h)
    assert not pw.verify_password("päss word 中文字符 ", h)


def test_boundary_lengths():
    with pytest.raises(pw.PasswordValidationError):
        pw.hash_password("12345678901")  # 11 < 12
    pw.hash_password("123456789012")  # 12 ok
    with pytest.raises(pw.PasswordValidationError):
        pw.hash_password("中" * 400)  # 400*3=1200 > 1024 bytes


def test_corrupt_format_fails_safely():
    assert not pw.verify_password("password1234", "garbage")
    assert not pw.verify_password("password1234", "scrypt$2$n=16384,r=8,p=5$aa$bb")
    assert not pw.verify_password("password1234", "bcrypt$1$aa$bb")
    assert not pw.verify_password("password1234", "scrypt$1$n=16384,r=8,p=5$aa")


def test_overlong_password_verify_fails_safely():
    h = pw.hash_password("123456789012")
    assert not pw.verify_password("中" * 400, h)


def test_dummy_verify_executes_scrypt(monkeypatch):
    calls = []
    original = pw.hashlib.scrypt

    def tracked_scrypt(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(pw.hashlib, "scrypt", tracked_scrypt)
    pw.dummy_verify()
    assert len(calls) == 1


def test_invalid_hash_types_and_unicode_fail_safely():
    assert not pw.verify_password("password1234", None)
    assert not pw.verify_password("\ud800" * 12, "not-a-hash")
    with pytest.raises(pw.PasswordValidationError):
        pw.hash_password("\ud800" * 12)


def test_password_request_repr_masks_secret():
    body = LoginRequest(username="alice", password="do-not-log-this")
    assert "do-not-log-this" not in repr(body)


# ---------------------------------------------------------------------------
# service 业务
# ---------------------------------------------------------------------------


def test_authenticate_success_and_failure(temp_user_db, secret):
    _mk_user(temp_user_db, "alice", "user")
    S = temp_user_db["Session"]
    s = S()
    try:
        u = service.authenticate(s, "alice", "password1234")
        assert u.username == "alice"
        with pytest.raises(service.AuthFailed):
            service.authenticate(s, "alice", "wrongpass999")
        with pytest.raises(service.AuthFailed):
            service.authenticate(s, "ghost-user", "password1234")
    finally:
        s.close()


def test_change_password_updates_version_and_flag(temp_user_db, secret):
    me = _mk_user(temp_user_db, "bob", "user", password="oldpassword1")
    S = temp_user_db["Session"]
    s = S()
    try:
        u = user_repo.get_by_id(s, me["id"])
        old_ver = u.auth_version
        service.change_password(s, u, "oldpassword1", "newpassword1")
        s.commit()
        u2 = user_repo.get_by_id(s, me["id"])
        assert u2.auth_version == old_ver + 1
        assert u2.must_change_password is False
        assert pw.verify_password("newpassword1", u2.password_hash)
        with pytest.raises(service.AuthFailed):
            service.authenticate(s, "bob", "oldpassword1")
    finally:
        s.close()


def test_change_password_wrong_old_raises(temp_user_db, secret):
    me = _mk_user(temp_user_db, "carol2", "user", password="oldpassword1")
    S = temp_user_db["Session"]
    s = S()
    try:
        u = user_repo.get_by_id(s, me["id"])
        with pytest.raises(service.AuthFailed):
            service.change_password(s, u, "wrongold", "newpassword1")
    finally:
        s.close()


def test_create_user_conflict(temp_user_db, secret):
    _mk_user(temp_user_db, "carol", "user")
    S = temp_user_db["Session"]
    s = S()
    try:
        with pytest.raises(service.UsernameConflict):
            service.create_user(
                s, username="carol", display_name="Carol", password="password1234",
            )
        created = service.create_user(
            s,
            username="carol2",
            display_name="Carol 2",
            password="password1234",
        )
        s.commit()
        assert created.username == "carol2"
    finally:
        s.close()


def test_create_user_rejects_overlong_display_name(temp_user_db):
    s = temp_user_db["Session"]()
    try:
        with pytest.raises(service.InvalidInput, match="100"):
            service.create_user(
                s,
                username="longname",
                display_name="名" * 101,
                password="password1234",
            )
    finally:
        s.close()


def test_set_status_disables_and_bumps_version(temp_user_db, secret):
    actor = _mk_user(temp_user_db, "boss", "admin")
    me = _mk_user(temp_user_db, "dave", "user", password="password1234")
    S = temp_user_db["Session"]
    s = S()
    try:
        actor_obj = user_repo.get_by_id(s, actor["id"])
        old_ver = me["auth_version"]
        service.set_status(s, actor_obj, me["id"], "disabled")
        s.commit()
        u2 = user_repo.get_by_id(s, me["id"])
        assert u2.status == "disabled"
        assert u2.auth_version == old_ver + 1
        with pytest.raises(service.AuthFailed):
            service.authenticate(s, "dave", "password1234")
    finally:
        s.close()


def test_set_status_forbids_self_disable(temp_user_db, secret):
    me = _mk_user(temp_user_db, "selfy", "admin")
    S = temp_user_db["Session"]
    s = S()
    try:
        actor = user_repo.get_by_id(s, me["id"])
        with pytest.raises(service.Forbidden):
            service.set_status(s, actor, me["id"], "disabled")
    finally:
        s.close()


def test_set_status_invalid_raises(temp_user_db, secret):
    me = _mk_user(temp_user_db, "erin", "user")
    S = temp_user_db["Session"]
    s = S()
    try:
        actor = user_repo.get_by_id(s, me["id"])
        with pytest.raises(service.InvalidInput):
            service.set_status(s, actor, me["id"], "bogus")
    finally:
        s.close()


def test_init_first_admin_creates_then_rejects(temp_user_db, secret):
    S = temp_user_db["Session"]
    s = S()
    try:
        u = service.init_first_admin(s, "root", "Root", "password1234")
        s.commit()
        assert u.role == "admin" and u.status == "active"
        with pytest.raises(service.NoAdminLeft):
            service.init_first_admin(s, "root2", "Root2", "password1234")
        s.rollback()
    finally:
        s.close()


# ---------------------------------------------------------------------------
# auth API
# ---------------------------------------------------------------------------


def test_login_success_returns_token_and_masked_user(client, temp_user_db, secret):
    _mk_user(temp_user_db, "alice", "user")
    r = client.post(
        "/auth/login",
        json={"username": "alice", "password": "password1234"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["token_type"] == "Bearer"
    assert data["expires_in"] == int(auth_jwt.get_token_ttl().total_seconds())
    u = data["user"]
    assert u["username"] == "alice" and u["role"] == "user"
    assert "password_hash" not in u and "password" not in u
    claims = _pyjwt.decode(data["token"], secret, algorithms=["HS256"])
    assert claims["sub"] == u["id"] and claims["iss"] == auth_jwt.ISSUER


def test_token_ttl_env_default_and_clamp(monkeypatch):
    """B7：CHATCHAT_AUTH_TTL_MINUTES 可配，默认 720 分钟，越界 clamp。"""
    env = auth_jwt.TOKEN_TTL_MINUTES_ENV
    monkeypatch.delenv(env, raising=False)
    assert auth_jwt.get_token_ttl() == timedelta(minutes=720)

    monkeypatch.setenv(env, "45")
    assert auth_jwt.get_token_ttl() == timedelta(minutes=45)

    monkeypatch.setenv(env, "1")
    assert auth_jwt.get_token_ttl() == timedelta(minutes=auth_jwt.MIN_TOKEN_TTL_MINUTES)

    monkeypatch.setenv(env, "99999")
    assert auth_jwt.get_token_ttl() == timedelta(minutes=auth_jwt.MAX_TOKEN_TTL_MINUTES)

    monkeypatch.setenv(env, "not-a-number")
    assert auth_jwt.get_token_ttl() == timedelta(minutes=auth_jwt.DEFAULT_TOKEN_TTL_MINUTES)


def test_login_expires_in_follows_ttl_env(client, temp_user_db, secret, monkeypatch):
    """登录响应的 expires_in 与令牌 exp 必须跟随环境变量。"""
    _mk_user(temp_user_db, "ttl-alice", "user")
    monkeypatch.setenv(auth_jwt.TOKEN_TTL_MINUTES_ENV, "45")
    r = client.post(
        "/auth/login",
        json={"username": "ttl-alice", "password": "password1234"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["expires_in"] == 45 * 60
    claims = _pyjwt.decode(data["token"], secret, algorithms=["HS256"])
    assert claims["exp"] - claims["iat"] == 45 * 60


def test_login_wrong_password_and_unknown_user_same_401(client, temp_user_db, secret):
    _mk_user(temp_user_db, "alice", "user")
    r1 = client.post("/auth/login", json={"username": "alice", "password": "nope999"})
    r2 = client.post(
        "/auth/login",
        json={"username": "no-such-user", "password": "nope999"},
    )
    assert r1.status_code == r2.status_code == 401
    assert r1.headers.get("WWW-Authenticate") == "Bearer"
    assert r2.headers.get("WWW-Authenticate") == "Bearer"
    assert r1.json()["detail"] == r2.json()["detail"]  # 通用信息一致


def test_disabled_user_cannot_login_and_token_invalidated(client, temp_user_db, secret):
    me = _mk_user(temp_user_db, "erin", "user")
    token = auth_jwt.create_token(me["id"], me["auth_version"])
    r = client.post(
        "/auth/login",
        json={"username": "erin", "password": "password1234"},
    )
    assert r.status_code == 200
    admin_h = _admin_headers(temp_user_db, secret)
    r2 = client.patch(
        f"/admin/users/{me['id']}/status", json={"status": "disabled"}, headers=admin_h,
    )
    assert r2.status_code == 200
    assert client.get("/auth/me", headers=_bearer(token)).status_code == 401
    r3 = client.post(
        "/auth/login",
        json={"username": "erin", "password": "password1234"},
    )
    assert r3.status_code == 401


def test_token_variants_return_401(client, temp_user_db, secret):
    me = _mk_user(temp_user_db, "frank", "user")
    now = datetime.now(timezone.utc)
    base = {
        "sub": me["id"], "ver": me["auth_version"], "iss": auth_jwt.ISSUER,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=30)).timestamp()),
    }
    valid = _encode_token(dict(base), secret)
    assert client.get("/auth/me", headers=_bearer(valid)).status_code == 200

    bad_sig = _encode_token(dict(base), "different-key-" + "0" * 30)
    assert client.get("/auth/me", headers=_bearer(bad_sig)).status_code == 401

    bad_iss = _encode_token({**base, "iss": "evil"}, secret)
    assert client.get("/auth/me", headers=_bearer(bad_iss)).status_code == 401

    expired = _encode_token(
        {**base, "exp": int((now - timedelta(minutes=1)).timestamp())},
        secret,
    )
    assert client.get("/auth/me", headers=_bearer(expired)).status_code == 401

    no_sub = {k: v for k, v in base.items() if k != "sub"}
    assert client.get(
        "/auth/me",
        headers=_bearer(_encode_token(no_sub, secret)),
    ).status_code == 401

    malformed_claims = [
        {**base, "sub": ["not", "a", "string"]},
        {**base, "sub": "not-a-uuid"},
        {**base, "sub": "0" * 32},
        {**base, "ver": "1"},
        {**base, "ver": True},
        {**base, "ver": me["auth_version"] + 1},
        {**base, "iat": "not-a-timestamp"},
    ]
    for payload in malformed_claims:
        token = _encode_token(payload, secret)
        assert client.get("/auth/me", headers=_bearer(token)).status_code == 401

    assert client.get("/auth/me").status_code == 401
    assert client.get(
        "/auth/me",
        headers={"Authorization": "Basic abc"},
    ).status_code == 401


def test_create_token_rejects_invalid_internal_claims(secret):
    with pytest.raises(ValueError):
        auth_jwt.create_token("not-a-uuid", 1)
    with pytest.raises(ValueError):
        auth_jwt.create_token("0" * 32, True)
    with pytest.raises(ValueError):
        auth_jwt.create_token("0" * 32, 0)


def test_login_fails_closed_without_valid_secret(
    client, temp_user_db, monkeypatch
):
    _mk_user(temp_user_db, "configtest", "user")
    monkeypatch.delenv("CHATCHAT_AUTH_SECRET", raising=False)
    missing = client.post(
        "/auth/login",
        json={"username": "configtest", "password": "password1234"},
    )
    assert missing.status_code == 500
    assert "配置错误" in missing.json()["detail"]
    assert "CHATCHAT_AUTH_SECRET" not in missing.text

    monkeypatch.setenv("CHATCHAT_AUTH_SECRET", "too-short")
    short = client.post(
        "/auth/login",
        json={"username": "configtest", "password": "password1234"},
    )
    assert short.status_code == 500
    assert "配置错误" in short.json()["detail"]
    assert "too-short" not in short.text


def test_change_password_invalidates_old_token(client, temp_user_db, secret):
    _mk_user(temp_user_db, "grace", "user")
    r = client.post(
        "/auth/login",
        json={"username": "grace", "password": "password1234"},
    )
    h = _bearer(r.json()["token"])
    r2 = client.post(
        "/auth/change-password",
        json={"old_password": "password1234", "password": "newpassword1"},
        headers=h,
    )
    assert r2.status_code == 200
    assert r2.json()["must_change_password"] is False
    assert client.get("/auth/me", headers=h).status_code == 401  # 旧 token 失效
    r3 = client.post(
        "/auth/login",
        json={"username": "grace", "password": "newpassword1"},
    )
    assert r3.status_code == 200


def test_change_password_requires_old_password(client, temp_user_db, secret):
    _mk_user(temp_user_db, "hank", "user")
    r = client.post(
        "/auth/login",
        json={"username": "hank", "password": "password1234"},
    )
    h = _bearer(r.json()["token"])
    r2 = client.post(
        "/auth/change-password",
        json={"old_password": "wrongold1", "password": "newpassword1"},
        headers=h,
    )
    assert r2.status_code == 401


def test_logout_idempotent_and_no_revocation_claim(client, temp_user_db, secret):
    _mk_user(temp_user_db, "irene", "user")
    r = client.post(
        "/auth/login",
        json={"username": "irene", "password": "password1234"},
    )
    h = _bearer(r.json()["token"])
    r1 = client.post("/auth/logout", headers=h)
    r2 = client.post("/auth/logout", headers=h)
    assert r1.status_code == r2.status_code == 200
    body = r1.json().get("message", "")
    assert "revoked" not in body.lower() and "撤销" not in body


def test_auth_me_masked(client, temp_user_db, secret):
    _mk_user(temp_user_db, "jack", "user")
    r = client.post(
        "/auth/login",
        json={"username": "jack", "password": "password1234"},
    )
    r2 = client.get("/auth/me", headers=_bearer(r.json()["token"]))
    assert r2.status_code == 200
    assert "password_hash" not in r2.text


# ---------------------------------------------------------------------------
# admin API
# ---------------------------------------------------------------------------


def test_admin_endpoints_forbidden_for_user(client, temp_user_db, secret):
    h = _user_headers(temp_user_db, secret)
    assert client.get("/admin/users", headers=h).status_code == 403
    assert client.post("/admin/users", json={
        "username": "x1", "display_name": "X", "password": "password1234"},
        headers=h).status_code == 403
    victim = _mk_user(temp_user_db, "victim", "user")
    assert client.patch(f"/admin/users/{victim['id']}/status",
        json={"status": "disabled"}, headers=h).status_code == 403
    assert client.post(f"/admin/users/{victim['id']}/reset-password",
        json={"password": "newpassword1"}, headers=h).status_code == 403


def test_admin_list_and_create(client, temp_user_db, secret):
    _mk_user(temp_user_db, "alice", "user")
    h = _admin_headers(temp_user_db, secret)
    r = client.get("/admin/users", headers=h)
    assert r.status_code == 200
    users = r.json()
    assert any(u["username"] == "alice" for u in users)
    assert all("password_hash" not in u and "password" not in u for u in users)

    r2 = client.post("/admin/users", json={
        "username": "bruno", "display_name": "Bruno", "password": "password1234",
    }, headers=h)
    assert r2.status_code == 201  # 创建成功 201
    assert r2.json()["username"] == "bruno"

    r3 = client.post("/admin/users", json={
        "username": "bruno", "display_name": "Bruno", "password": "password1234",
    }, headers=h)
    assert r3.status_code == 409  # 重复

    r4 = client.post("/admin/users", json={
        "username": "ab", "display_name": "Ab", "password": "password1234",
    }, headers=h)
    assert r4.status_code == 422  # 非法 username

    invalid_role = client.post(
        "/admin/users",
        json={
            "username": "invalidrole",
            "display_name": "Invalid Role",
            "password": "password1234",
            "role": "owner",
        },
        headers=h,
    )
    assert invalid_role.status_code == 422


def test_temporary_password_admin_must_change_before_management(
    client, temp_user_db, secret
):
    existing_admin = _admin_headers(temp_user_db, secret)
    created = client.post(
        "/admin/users",
        json={
            "username": "newadmin",
            "display_name": "New Admin",
            "password": "temporary1234",
            "role": "admin",
        },
        headers=existing_admin,
    )
    assert created.status_code == 201

    login = client.post(
        "/auth/login",
        json={"username": "newadmin", "password": "temporary1234"},
    )
    temporary_headers = _bearer(login.json()["token"])
    blocked = client.get("/admin/users", headers=temporary_headers)
    assert blocked.status_code == 403
    assert "修改密码" in blocked.json()["detail"]

    changed = client.post(
        "/auth/change-password",
        json={"old_password": "temporary1234", "password": "permanent1234"},
        headers=temporary_headers,
    )
    assert changed.status_code == 200
    relogin = client.post(
        "/auth/login",
        json={"username": "newadmin", "password": "permanent1234"},
    )
    assert client.get(
        "/admin/users",
        headers=_bearer(relogin.json()["token"]),
    ).status_code == 200


def test_admin_set_status(client, temp_user_db, secret):
    me = _mk_user(temp_user_db, "clara", "user")
    h = _admin_headers(temp_user_db, secret)
    r = client.patch(f"/admin/users/{me['id']}/status",
        json={"status": "disabled"}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "disabled"
    r2 = client.patch("/admin/users/0000000000000000000000000000dead",
        json={"status": "disabled"}, headers=h)
    assert r2.status_code == 404
    r3 = client.patch(f"/admin/users/{me['id']}/status",
        json={"status": "bogus"}, headers=h)
    assert r3.status_code == 422
    enabled = client.patch(
        f"/admin/users/{me['id']}/status",
        json={"status": "active"},
        headers=h,
    )
    assert enabled.status_code == 200
    assert enabled.json()["status"] == "active"
    assert client.post(
        "/auth/login",
        json={"username": "clara", "password": "password1234"},
    ).status_code == 200


def test_admin_cannot_disable_self(client, temp_user_db, secret):
    admin = _mk_user(temp_user_db, "onlyadmin", "admin")
    headers = _bearer(
        auth_jwt.create_token(admin["id"], admin["auth_version"])
    )
    response = client.patch(
        f"/admin/users/{admin['id']}/status",
        json={"status": "disabled"},
        headers=headers,
    )
    assert response.status_code == 403
    assert client.get("/auth/me", headers=headers).status_code == 200


def test_admin_reset_password_invalidates_token(client, temp_user_db, secret):
    me = _mk_user(temp_user_db, "dmitri", "user")
    r = client.post(
        "/auth/login",
        json={"username": "dmitri", "password": "password1234"},
    )
    old_token = r.json()["token"]
    h = _admin_headers(temp_user_db, secret)
    r2 = client.post(f"/admin/users/{me['id']}/reset-password",
        json={"password": "resetpassword1"}, headers=h)
    assert r2.status_code == 200
    assert r2.json()["must_change_password"] is True
    assert client.get("/auth/me", headers=_bearer(old_token)).status_code == 401
    r3 = client.post(
        "/auth/login",
        json={"username": "dmitri", "password": "resetpassword1"},
    )
    assert r3.status_code == 200
    r4 = client.post("/admin/users/0000000000000000000000000000dead/reset-password",
        json={"password": "resetpassword1"}, headers=h)
    assert r4.status_code == 404


# ---------------------------------------------------------------------------
# 首个管理员 CLI
# ---------------------------------------------------------------------------


def _migrate_cli_db(tmp_path):
    path = tmp_path / "cli.db"
    path.touch()
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        apply_migrations(build_registry(), conn)
    return engine, path


def _patch_cli(monkeypatch, path):
    """让 CLI 在调用时从当前设置创建指向临时库的 engine/session。"""
    from chatchat import settings as settings_module

    monkeypatch.setattr(
        settings_module.Settings.basic_settings,
        "SQLALCHEMY_DATABASE_URI",
        f"sqlite:///{path}",
    )


def test_cli_init_admin_success(monkeypatch, tmp_path):
    from chatchat.server.auth.cli_users import users

    engine, path = _migrate_cli_db(tmp_path)
    _patch_cli(monkeypatch, path)
    result = CliRunner().invoke(
        users, ["init-admin"],
        input="root\nRoot Admin\nrootpassword1234\nrootpassword1234\n",
    )
    assert result.exit_code == 0, result.output
    assert "root" in result.output
    assert "rootpassword1234" not in result.output  # 不回显密码
    assert "scrypt" not in result.output  # 不回显 hash
    with engine.connect() as conn:
        n = conn.execute(text(
            "SELECT COUNT(*) FROM user_account WHERE role='admin'"
        )).scalar()
    assert n == 1
    engine.dispose()


def test_cli_init_admin_mismatched_password_fails(monkeypatch, tmp_path):
    from chatchat.server.auth.cli_users import users

    engine, path = _migrate_cli_db(tmp_path)
    _patch_cli(monkeypatch, path)
    result = CliRunner().invoke(
        users, ["init-admin"],
        input="root\nRoot\nrootpassword1234\ndifferent456\n",
    )
    assert result.exit_code != 0
    with engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM user_account")).scalar()
    assert n == 0
    engine.dispose()


def test_cli_init_admin_refuses_when_admin_exists(monkeypatch, tmp_path):
    from chatchat.server.auth.cli_users import users

    engine, path = _migrate_cli_db(tmp_path)
    _patch_cli(monkeypatch, path)
    # 预置一个 admin
    S = sessionmaker(bind=engine)
    s = S()
    service.init_first_admin(s, "root", "Root", "rootpassword1234")
    s.commit()
    s.close()
    result = CliRunner().invoke(
        users, ["init-admin"],
        input="root2\nRoot2\nrootpassword1234\nrootpassword1234\n",
    )
    assert result.exit_code != 0
    assert "管理员" in result.output or "admin" in result.output.lower()
    engine.dispose()


def test_cli_init_admin_unmigrated_db_gives_actionable_error(monkeypatch, tmp_path):
    from chatchat.server.auth.cli_users import users

    path = tmp_path / "cli.db"
    _patch_cli(monkeypatch, path)
    result = CliRunner().invoke(
        users, ["init-admin"],
        input="root\nRoot\nrootpassword1234\nrootpassword1234\n",
    )
    assert result.exit_code != 0
    assert "migrate upgrade" in result.output
    assert not path.exists()


def test_cli_init_admin_rejects_unversioned_manual_table(
    monkeypatch, tmp_path
):
    from chatchat.server.auth.cli_users import users

    path = tmp_path / "manual.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE user_account (id VARCHAR(32))")
    _patch_cli(monkeypatch, path)
    result = CliRunner().invoke(users, ["init-admin"])
    assert result.exit_code != 0
    assert "migrate upgrade" in result.output


def test_cli_module_import_creates_no_database(monkeypatch, tmp_path):
    """导入 CLI 模块不创建数据库或用户。"""
    from chatchat import settings as settings_module

    db = tmp_path / "fresh.db"
    monkeypatch.setattr(
        settings_module.Settings.basic_settings,
        "SQLALCHEMY_DATABASE_URI",
        f"sqlite:///{db}",
    )
    import importlib
    import chatchat.server.auth.cli_users as cli_mod
    importlib.reload(cli_mod)
    assert not db.exists()


def test_legacy_create_tables_does_not_bypass_v2_migration(tmp_path):
    """旧 create_tables 不能绕过 v2 迁移创建用户表。"""
    code = """
import sqlite3
from chatchat.settings import Settings
Settings.basic_settings.make_dirs()
from chatchat.server.knowledge_base.migrate import create_tables
create_tables()
with sqlite3.connect(Settings.basic_settings.DB_ROOT_PATH) as conn:
    names = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
raise SystemExit(1 if "user_account" in names else 0)
"""
    env = os.environ.copy()
    env["CHATCHAT_ROOT"] = str(tmp_path / "isolated-root")
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
