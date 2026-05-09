from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

try:
    from zoneinfo import ZoneInfo
except ImportError:
    def ZoneInfo(_: str) -> timezone:
        return timezone(timedelta(hours=8))

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles


APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("SITE_DB_PATH", APP_DIR / "data" / "site.db"))
DOWNLOAD_DIR = Path(os.getenv("SITE_DOWNLOAD_DIR", APP_DIR / "data" / "downloads"))
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
ADMIN_USER = os.getenv("SITE_ADMIN_USER", "fyanxv")
ADMIN_PASSWORD = os.getenv("SITE_ADMIN_PASSWORD", "change-me")
DOWNLOAD_PLATFORMS = {"windows", "macos", "linux"}

app = FastAPI(title="云逸中转站")
security = HTTPBasic()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def db() -> Any:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            create table if not exists config (
                key text primary key,
                value text not null
            )
            """
        )
        conn.execute(
            """
            create table if not exists download_packages (
                id integer primary key autoincrement,
                platform text not null default 'windows',
                filename text not null unique,
                original_name text not null,
                size_bytes integer not null default 0,
                uploaded_at text not null,
                active integer not null default 0
            )
            """
        )
        try:
            conn.execute("alter table download_packages add column platform text not null default 'windows'")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise


@app.on_event("startup")
async def startup() -> None:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    init_db()


def get_config(key: str, fallback: Any = None) -> Any:
    with db() as conn:
        row = conn.execute("select value from config where key = ?", (key,)).fetchone()
    if not row:
        return fallback
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return fallback


def set_config(key: str, value: Any) -> None:
    with db() as conn:
        conn.execute(
            """
            insert into config(key, value) values(?, ?)
            on conflict(key) do update set value = excluded.value
            """,
            (key, json.dumps(value, ensure_ascii=False)),
        )


def now_text() -> str:
    return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def require_admin(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    valid_user = secrets.compare_digest(credentials.username, ADMIN_USER)
    valid_pass = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not (valid_user and valid_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def row_to_download_package(row: sqlite3.Row) -> Dict[str, Any]:
    filename = row["filename"]
    return {
        "id": row["id"],
        "platform": row["platform"] if "platform" in row.keys() else "windows",
        "filename": filename,
        "originalName": row["original_name"],
        "sizeBytes": row["size_bytes"],
        "uploadedAt": row["uploaded_at"],
        "active": bool(row["active"]),
        "downloadUrl": f"/downloads/{filename}",
    }


def list_download_packages() -> list[Dict[str, Any]]:
    init_db()
    with db() as conn:
        rows = conn.execute(
            """
            select * from download_packages
            order by active desc, platform asc, uploaded_at desc, id desc
            """
        ).fetchall()
    return [row_to_download_package(row) for row in rows]


def active_download_package(platform: str = "windows") -> Optional[Dict[str, Any]]:
    platform = normalize_platform(platform)
    init_db()
    with db() as conn:
        row = conn.execute(
            """
            select * from download_packages
            where active = 1 and platform = ?
            order by uploaded_at desc, id desc
            limit 1
            """
            ,
            (platform,),
        ).fetchone()
    return row_to_download_package(row) if row else None


def active_download_packages() -> Dict[str, Dict[str, Any]]:
    return {
        platform: package
        for platform in ("windows", "macos", "linux")
        if (package := active_download_package(platform))
    }


def normalize_platform(platform: str) -> str:
    value = str(platform or "").strip().lower()
    if value in {"mac", "darwin", "osx"}:
        return "macos"
    if value not in DOWNLOAD_PLATFORMS:
        return "windows"
    return value


def download_target(filename: str) -> Path:
    target = (DOWNLOAD_DIR / filename).resolve()
    root = DOWNLOAD_DIR.resolve()
    if root != target and root not in target.parents:
        raise HTTPException(status_code=404, detail="download not found")
    return target


def ensure_download_package_record(settings: Dict[str, Any]) -> None:
    filename = str(settings.get("downloadFilename") or "").strip()
    if not filename:
        return
    target = download_target(filename)
    if not target.exists() or not target.is_file():
        return
    with db() as conn:
        existing = conn.execute(
            "select id from download_packages where filename = ?",
            (filename,),
        ).fetchone()
        if existing:
            return
        has_active = conn.execute(
            "select count(*) as c from download_packages where active = 1 and platform = 'windows'"
        ).fetchone()["c"]
        conn.execute(
            """
            insert into download_packages(platform, filename, original_name, size_bytes, uploaded_at, active)
            values('windows', ?, ?, ?, ?, ?)
            """,
            (filename, filename, target.stat().st_size, now_text(), 0 if has_active else 1),
        )


def default_site_settings() -> Dict[str, Any]:
    return {
        "brandName": "云逸",
        "siteDomain": "yunyi.hstudy.xyz",
        "announcement": "导入前请先安装 CC-Switch。卡密会在浏览器本地生成导入链接并写入本机 CC-Switch；导入后如未生效，请在 CC-Switch 中手动激活对应通道。",
        "buyUrl": "https://pay.ldxp.cn/shop/Q5L5OORI",
        "balanceApiUrl": "https://yunyi.cfd/user/api/v1/me?view=dashboard",
        "modelTrendsApiUrl": "https://yunyi.cfd/user/api/v1/usage/model-trends?period=24h",
        "usageHistoryApiUrl": "https://yunyi.cfd/user/api/v1/usage/history?period=24h",
        "downloadLabel": "下载 CC-Switch",
        "downloadFilename": "CC-Switch-v3.14.1-Windows_8.msi",
        "downloadUrl": "",
        "claudeName": "云逸 Claude",
        "claudeEndpoint": "https://yunyi.cfd/claude",
        "claudeHomepage": "https://yunyi.cfd",
        "codexName": "云逸 Codex",
        "codexEndpoint": "https://yunyi.cfd/codex",
        "codexHomepage": "https://yunyi.cfd",
    }


def get_site_settings() -> Dict[str, Any]:
    saved = get_config("site_settings", {}) or {}
    settings = {**default_site_settings(), **saved}
    settings.pop("downloadPackage", None)
    settings.pop("downloadPackages", None)
    settings.pop("downloadUrl", None)
    ensure_download_package_record(settings)
    packages = active_download_packages()
    package = packages.get("windows") or next(iter(packages.values()), None)
    if package:
        settings["downloadFilename"] = package["filename"]
        settings["downloadPackage"] = package
    settings["downloadPackages"] = packages
    filename = str(settings.get("downloadFilename") or "").strip()
    settings["downloadUrl"] = f"/downloads/{filename}" if filename else ""
    return settings


def save_site_settings(settings: Dict[str, Any]) -> None:
    clean = dict(settings)
    clean.pop("downloadPackage", None)
    clean.pop("downloadPackages", None)
    clean.pop("downloadUrl", None)
    set_config("site_settings", clean)


def public_site_settings() -> Dict[str, Any]:
    settings = get_site_settings()
    settings.pop("balanceApiUrl", None)
    settings.pop("modelTrendsApiUrl", None)
    settings.pop("usageHistoryApiUrl", None)
    return settings


def parse_iso_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def clean_balance_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    quota = payload.get("quota") if isinstance(payload.get("quota"), dict) else {}
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    timestamps = payload.get("timestamps") if isinstance(payload.get("timestamps"), dict) else {}
    billing_type = str(payload.get("billing_type") or quota.get("type") or "")
    if billing_type == "duration":
        total_quota = quota.get("daily_quota", 0)
        remaining_quota = quota.get("daily_remaining", 0)
        used_quota = quota.get("daily_spent", usage.get("daily_spent", 0))
    else:
        total_quota = quota.get("total_quota", 0)
        remaining_quota = quota.get("remaining_quota", 0)
        used_quota = quota.get("used_quota", usage.get("total_spent", 0))
    used_percent = quota.get("daily_used") if billing_type == "duration" else quota.get("used_percent")
    if used_percent is None and total_quota:
        used_percent = round(float(used_quota or 0) / float(total_quota) * 100)
    expires_at = str(timestamps.get("expires_at") or "")
    expiry = parse_iso_datetime(expires_at)
    remaining_seconds = None
    if expiry:
        remaining_seconds = max(0, int((expiry - datetime.now(timezone.utc)).total_seconds()))

    return {
        "keyPreview": payload.get("key_preview", ""),
        "status": payload.get("status", ""),
        "serviceType": payload.get("service_type", ""),
        "planName": payload.get("sub_service_type_name", ""),
        "billingType": billing_type,
        "quota": {
            "type": quota.get("type", billing_type),
            "dailyQuota": quota.get("daily_quota", 0),
            "dailySpent": quota.get("daily_spent", 0),
            "dailyTotalSpent": quota.get("daily_total_spent", 0),
            "dailyRemaining": quota.get("daily_remaining", 0),
            "dailyUsedPercent": quota.get("daily_used", 0),
            "nextResetAt": quota.get("next_reset_at", ""),
            "resetTimezone": quota.get("reset_timezone", ""),
            "total": total_quota,
            "remaining": remaining_quota,
            "used": used_quota,
            "usedPercent": used_percent or 0,
            "remainingCount": quota.get("remaining_count", 0),
            "quotaPackRemaining": quota.get("quota_pack_remaining", 0),
            "requestsPackRemaining": quota.get("requests_pack_remaining", 0),
        },
        "usage": {
            "totalSpent": usage.get("total_spent", 0),
            "dailySpent": usage.get("daily_spent", 0),
            "requestCount": usage.get("request_count", 0),
            "dailyRequestCount": usage.get("daily_request_count", 0),
            "inputTokens": usage.get("input_tokens", 0),
            "outputTokens": usage.get("output_tokens", 0),
            "cacheReadTokens": usage.get("cache_read_tokens", 0),
            "cacheWriteTokens": usage.get("cache_write_tokens", 0),
            "totalTokens": usage.get("total_tokens", 0),
        },
        "timestamps": {
            "activatedAt": timestamps.get("activated_at", ""),
            "expiresAt": expires_at,
            "lastUsedAt": timestamps.get("last_used_at", ""),
            "validityDays": timestamps.get("validity_days", 0),
            "remainingSeconds": remaining_seconds,
        },
    }


def clean_model_trends_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    rows = payload.get("data") if isinstance(payload.get("data"), list) else []
    totals: Dict[str, Dict[str, Any]] = {}
    hourly = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        models = row.get("models") if isinstance(row.get("models"), list) else []
        clean_models = []
        for item in models:
            if not isinstance(item, dict):
                continue
            model = str(item.get("model") or "unknown")
            tokens = int(item.get("total_tokens") or 0)
            cost = int(item.get("cost") or 0)
            requests = int(item.get("requests") or 0)
            clean_item = {
                "model": model,
                "totalTokens": tokens,
                "cost": cost,
                "requests": requests,
            }
            clean_models.append(clean_item)
            bucket = totals.setdefault(model, {"model": model, "totalTokens": 0, "cost": 0, "requests": 0})
            bucket["totalTokens"] += tokens
            bucket["cost"] += cost
            bucket["requests"] += requests
        if clean_models:
            hourly.append({"date": row.get("date", ""), "models": clean_models})
    ranking = sorted(totals.values(), key=lambda item: (item["cost"], item["totalTokens"]), reverse=True)
    return {
        "period": payload.get("period", "24h"),
        "keyCount": payload.get("key_count", 1),
        "models": ranking,
        "hourly": hourly,
    }


def clean_usage_history_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    rows = payload.get("data") if isinstance(payload.get("data"), list) else []
    hourly = []
    totals = {
        "inputTokens": 0,
        "outputTokens": 0,
        "cacheReadTokens": 0,
        "cacheWriteTokens": 0,
        "totalTokens": 0,
        "requests": 0,
        "cost": 0,
    }
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = {
            "date": row.get("date", ""),
            "inputTokens": int(row.get("input_tokens") or 0),
            "outputTokens": int(row.get("output_tokens") or 0),
            "cacheReadTokens": int(row.get("cache_read_tokens") or 0),
            "cacheWriteTokens": int(row.get("cache_write_tokens") or row.get("cache_create_tokens") or 0),
            "totalTokens": int(row.get("total_tokens") or 0),
            "requests": int(row.get("requests") or 0),
            "cost": int(row.get("cost") or 0),
        }
        for key in totals:
            totals[key] += int(item.get(key) or 0)
        hourly.append(item)
    return {
        "period": payload.get("period", "24h"),
        "keyCount": payload.get("key_count", 1),
        "totals": totals,
        "hourly": hourly,
    }


def validate_upstream_https(url: str, label: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise HTTPException(status_code=500, detail=f"{label} must use https")


def validate_http_url(url: str, label: str, *, require_https: bool = False) -> None:
    if not url:
        return
    parsed = urlparse(url)
    allowed = {"https"} if require_https else {"http", "https"}
    if parsed.scheme not in allowed or not parsed.netloc:
        scheme = "https" if require_https else "http(s)"
        raise HTTPException(status_code=400, detail=f"{label} must be a valid {scheme} url")


async def get_upstream_with_retry(
    client: httpx.AsyncClient,
    url: str,
    headers: Dict[str, str],
    label: str,
    *,
    attempts: int = 3,
) -> httpx.Response:
    last_error: Exception | None = None
    last_response: httpx.Response | None = None
    for attempt in range(attempts):
        try:
            response = await client.get(url, headers=headers)
            if response.status_code < 500:
                return response
            last_response = response
        except httpx.HTTPError as exc:
            last_error = exc
        if attempt < attempts - 1:
            await asyncio.sleep(0.4 * (attempt + 1))
    if last_response is not None:
        return last_response
    raise HTTPException(status_code=502, detail=f"{label} query failed") from last_error


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(APP_DIR / "static" / "index.html")


@app.get("/fyanxv")
async def admin() -> FileResponse:
    return FileResponse(APP_DIR / "static" / "admin.html")


@app.get("/tutorial")
async def tutorial() -> FileResponse:
    return FileResponse(APP_DIR / "static" / "tutorial.html")


@app.get("/balance")
async def balance() -> FileResponse:
    return FileResponse(APP_DIR / "static" / "balance.html")


app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")


@app.get("/downloads/{filename}")
async def download_file(filename: str) -> FileResponse:
    target = download_target(filename)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="download not found")
    return FileResponse(target, filename=target.name)


@app.get("/api/site-settings")
async def api_site_settings() -> Dict[str, Any]:
    return {"settings": public_site_settings()}


@app.post("/api/balance")
async def api_balance(payload: Dict[str, Any]) -> Dict[str, Any]:
    api_key = str(payload.get("apiKey") or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="api key required")
    settings = get_site_settings()
    balance_api_url = str(settings.get("balanceApiUrl") or default_site_settings()["balanceApiUrl"]).strip()
    model_trends_api_url = str(settings.get("modelTrendsApiUrl") or default_site_settings()["modelTrendsApiUrl"]).strip()
    usage_history_api_url = str(settings.get("usageHistoryApiUrl") or default_site_settings()["usageHistoryApiUrl"]).strip()
    validate_upstream_https(balance_api_url, "balance api url")
    validate_upstream_https(model_trends_api_url, "model trends api url")
    validate_upstream_https(usage_history_api_url, "usage history api url")
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "yunyi-site/1.0",
    }
    async with httpx.AsyncClient(timeout=12) as client:
        upstream = await get_upstream_with_retry(client, balance_api_url, headers, "balance")
        try:
            trends_upstream = await get_upstream_with_retry(client, model_trends_api_url, headers, "model trends", attempts=2)
        except HTTPException:
            trends_upstream = None
        try:
            history_upstream = await get_upstream_with_retry(client, usage_history_api_url, headers, "usage history", attempts=2)
        except HTTPException:
            history_upstream = None
    if upstream.status_code in {401, 403}:
        raise HTTPException(status_code=401, detail="卡密无效或无权查询")
    if upstream.status_code >= 400:
        raise HTTPException(status_code=502, detail="上游查询繁忙，请稍后重试")
    try:
        data = upstream.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="上级余额接口返回格式异常") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="上级余额接口返回格式异常")
    trends = {}
    history = {}
    if trends_upstream is not None and trends_upstream.status_code == 200:
        try:
            trends_data = trends_upstream.json()
            if isinstance(trends_data, dict):
                trends = clean_model_trends_payload(trends_data)
        except ValueError:
            trends = {}
    if history_upstream is not None and history_upstream.status_code == 200:
        try:
            history_data = history_upstream.json()
            if isinstance(history_data, dict):
                history = clean_usage_history_payload(history_data)
        except ValueError:
            history = {}
    return {"balance": clean_balance_payload(data), "modelTrends": trends, "usageHistory": history}


@app.get("/api/admin/site-settings")
async def api_admin_site_settings(_: str = Depends(require_admin)) -> Dict[str, Any]:
    return {"settings": get_site_settings()}


@app.put("/api/admin/site-settings")
async def api_admin_update_site_settings(payload: Dict[str, Any], _: str = Depends(require_admin)) -> Dict[str, Any]:
    current = get_site_settings()
    balance_api_url = str(payload.get("balanceApiUrl") or current.get("balanceApiUrl", default_site_settings()["balanceApiUrl"])).strip()
    model_trends_api_url = str(payload.get("modelTrendsApiUrl") or current.get("modelTrendsApiUrl", default_site_settings()["modelTrendsApiUrl"])).strip()
    usage_history_api_url = str(payload.get("usageHistoryApiUrl") or current.get("usageHistoryApiUrl", default_site_settings()["usageHistoryApiUrl"])).strip()
    buy_url = str(payload.get("buyUrl") or "").strip()
    claude_endpoint = str(payload.get("claudeEndpoint") or current["claudeEndpoint"]).strip() or current["claudeEndpoint"]
    claude_homepage = str(payload.get("claudeHomepage") or current.get("claudeHomepage", "")).strip()
    codex_endpoint = str(payload.get("codexEndpoint") or current["codexEndpoint"]).strip() or current["codexEndpoint"]
    codex_homepage = str(payload.get("codexHomepage") or current.get("codexHomepage", "")).strip()
    validate_http_url(buy_url, "buy url")
    validate_http_url(balance_api_url, "balance api url", require_https=True)
    validate_http_url(model_trends_api_url, "model trends api url", require_https=True)
    validate_http_url(usage_history_api_url, "usage history api url", require_https=True)
    validate_http_url(claude_endpoint, "claude endpoint", require_https=True)
    validate_http_url(claude_homepage, "claude homepage")
    validate_http_url(codex_endpoint, "codex endpoint", require_https=True)
    validate_http_url(codex_homepage, "codex homepage")
    updated = {
        **current,
        "brandName": str(payload.get("brandName") or current["brandName"]).strip() or current["brandName"],
        "siteDomain": str(payload.get("siteDomain") or current["siteDomain"]).strip() or current["siteDomain"],
        "announcement": str(payload.get("announcement") or "").strip(),
        "buyUrl": buy_url,
        "balanceApiUrl": balance_api_url,
        "modelTrendsApiUrl": model_trends_api_url,
        "usageHistoryApiUrl": usage_history_api_url,
        "downloadLabel": str(payload.get("downloadLabel") or current["downloadLabel"]).strip() or current["downloadLabel"],
        "claudeName": str(payload.get("claudeName") or current["claudeName"]).strip() or current["claudeName"],
        "claudeEndpoint": claude_endpoint,
        "claudeHomepage": claude_homepage,
        "codexName": str(payload.get("codexName") or current["codexName"]).strip() or current["codexName"],
        "codexEndpoint": codex_endpoint,
        "codexHomepage": codex_homepage,
        "downloadFilename": current.get("downloadFilename", ""),
        "downloadUrl": current.get("downloadUrl", ""),
        "updatedAt": now_text(),
    }
    save_site_settings(updated)
    return {"settings": get_site_settings()}


@app.get("/api/admin/download-packages")
async def api_admin_download_packages(_: str = Depends(require_admin)) -> Dict[str, Any]:
    return {"items": list_download_packages(), "active": active_download_packages()}


@app.post("/api/admin/download-package")
async def api_admin_upload_download_package(
    file: UploadFile = File(...),
    platform: str = Form("windows"),
    _: str = Depends(require_admin),
) -> Dict[str, Any]:
    if not file.filename:
        raise HTTPException(status_code=400, detail="file required")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".msi", ".zip", ".exe", ".dmg", ".pkg", ".deb", ".rpm", ".appimage"}:
        raise HTTPException(status_code=400, detail="unsupported file type")
    package_platform = normalize_platform(platform)
    original_name = Path(file.filename).name
    safe_name = f"{uuid.uuid4().hex}{suffix}"
    target = download_target(safe_name)
    with target.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)
    size_bytes = target.stat().st_size
    with db() as conn:
        conn.execute("update download_packages set active = 0 where platform = ?", (package_platform,))
        conn.execute(
            """
            insert into download_packages(platform, filename, original_name, size_bytes, uploaded_at, active)
            values(?, ?, ?, ?, ?, 1)
            """,
            (package_platform, safe_name, original_name, size_bytes, now_text()),
        )
    settings = get_site_settings()
    if package_platform == "windows" or not settings.get("downloadFilename"):
        settings["downloadFilename"] = safe_name
    settings["updatedAt"] = now_text()
    save_site_settings(settings)
    return {"settings": get_site_settings(), "packages": list_download_packages()}


@app.post("/api/admin/download-package/{package_id}/activate")
async def api_admin_activate_download_package(package_id: int, _: str = Depends(require_admin)) -> Dict[str, Any]:
    with db() as conn:
        row = conn.execute("select * from download_packages where id = ?", (package_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="package not found")
        target = download_target(row["filename"])
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="package file missing")
        package_platform = normalize_platform(row["platform"] if "platform" in row.keys() else "windows")
        conn.execute("update download_packages set active = 0 where platform = ?", (package_platform,))
        conn.execute("update download_packages set active = 1 where id = ?", (package_id,))
    settings = get_site_settings()
    if package_platform == "windows" or not settings.get("downloadFilename"):
        settings["downloadFilename"] = row["filename"]
    settings["updatedAt"] = now_text()
    save_site_settings(settings)
    return {"settings": get_site_settings(), "packages": list_download_packages()}


@app.delete("/api/admin/download-package/{package_id}")
async def api_admin_delete_download_package(package_id: int, _: str = Depends(require_admin)) -> Dict[str, Any]:
    with db() as conn:
        row = conn.execute("select * from download_packages where id = ?", (package_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="package not found")
        if row["active"]:
            raise HTTPException(status_code=400, detail="active package cannot be deleted")
        conn.execute("delete from download_packages where id = ?", (package_id,))
    target = download_target(row["filename"])
    if target.exists() and target.is_file():
        target.unlink()
    return {"items": list_download_packages(), "active": active_download_packages()}
