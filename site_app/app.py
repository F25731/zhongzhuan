from __future__ import annotations

import json
import asyncio
import mimetypes
import os
import secrets
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    def ZoneInfo(_: str) -> timezone:
        return timezone(timedelta(hours=8))

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles


APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
DB_PATH = Path(os.getenv("SITE_DB_PATH", APP_DIR / "data" / "site.db"))
UPLOAD_DIR = Path(os.getenv("SITE_UPLOAD_DIR", APP_DIR / "data" / "uploads"))
DOWNLOAD_DIR = Path(os.getenv("SITE_DOWNLOAD_DIR", APP_DIR / "data" / "downloads"))
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
ENABLE_SIDEWORK_SYNC = os.getenv("SITE_ENABLE_SIDEWORK_SYNC", "false").lower() in {"1", "true", "yes", "on"}
ADMIN_USER = os.getenv("SITE_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("SITE_ADMIN_PASSWORD", "change-me")
security = HTTPBasic()

PAN_LABELS = {
    "quark": "夸克网盘",
    "baidu": "百度网盘",
    "guangya": "光鸭网盘",
    "xunlei": "迅雷网盘",
}
PAN_FIELDS = {
    "夸克网盘": "panLinkQuark",
    "百度网盘": "panLinkBaidu",
    "光鸭网盘": "panLinkGuangya",
    "迅雷网盘": "panLinkXunlei",
}
DEFAULT_CATEGORIES = ["课程", "模板", "素材", "软件", "影视", "电子书", "副业"]

app = FastAPI(title="Resource Site")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
sidework_sync_task: Optional[asyncio.Task] = None


@contextmanager
def db() -> Any:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("pragma foreign_keys = on")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now_sql() -> str:
    return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def today_range_sql() -> tuple[str, str]:
    start = datetime.now(BEIJING_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            create table if not exists categories (
                id integer primary key autoincrement,
                name text not null unique,
                sort_order integer not null default 0,
                created_at text not null default current_timestamp
            )
            """
        )
        conn.execute(
            """
            create table if not exists resources (
                id integer primary key autoincrement,
                title text not null,
                category text not null,
                intro text not null default '',
                image_path text not null default '',
                source text not null default 'manual',
                external_id text not null default '',
                click_count integer not null default 0,
                link_click_count integer not null default 0,
                created_at text not null default current_timestamp,
                updated_at text not null default current_timestamp
            )
            """
        )
        for statement in (
            "alter table resources add column click_count integer not null default 0",
            "alter table resources add column link_click_count integer not null default 0",
        ):
            try:
                conn.execute(statement)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        conn.execute(
            """
            create unique index if not exists idx_resources_external
            on resources(source, external_id)
            where external_id != ''
            """
        )
        conn.execute(
            """
            create table if not exists resource_links (
                id integer primary key autoincrement,
                resource_id integer not null references resources(id) on delete cascade,
                pan_type text not null,
                url text not null,
                code text not null default '',
                unique(resource_id, pan_type)
            )
            """
        )
        conn.execute(
            """
            create table if not exists resource_clicks (
                id integer primary key autoincrement,
                resource_id integer not null references resources(id) on delete cascade,
                click_type text not null,
                created_at text not null
            )
            """
        )
        conn.execute(
            """
            create table if not exists requests (
                id integer primary key autoincrement,
                email text not null,
                intro text not null,
                image_path text not null default '',
                created_at text not null default current_timestamp
            )
            """
        )
        conn.execute(
            """
            create table if not exists request_images (
                id integer primary key autoincrement,
                request_id integer not null references requests(id) on delete cascade,
                image_path text not null,
                created_at text not null default current_timestamp
            )
            """
        )
        conn.execute(
            """
            insert into request_images(request_id, image_path)
            select requests.id, requests.image_path
            from requests
            where requests.image_path != ''
              and not exists (
                select 1 from request_images
                where request_images.request_id = requests.id
                  and request_images.image_path = requests.image_path
              )
            """
        )
        conn.execute(
            """
            create table if not exists config (
                key text primary key,
                value text not null
            )
            """
        )

        count = conn.execute("select count(*) as c from categories").fetchone()["c"]
        if not count:
            for index, name in enumerate(DEFAULT_CATEGORIES):
                conn.execute(
                    "insert into categories(name, sort_order) values(?, ?)",
                    (name, index),
                )


@app.on_event("startup")
async def startup() -> None:
    global sidework_sync_task
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    if ENABLE_SIDEWORK_SYNC:
        sidework_sync_task = asyncio.create_task(sidework_sync_loop())


@app.on_event("shutdown")
async def shutdown() -> None:
    if sidework_sync_task:
        sidework_sync_task.cancel()


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


def default_site_settings() -> Dict[str, Any]:
    return {
        "brandName": "云逸",
        "siteDomain": "yunyi.hstudy.xyz",
        "announcement": "导入前请先安装 CC-Switch。卡密会在浏览器本地生成导入链接并写入本机 CC-Switch；导入后如未生效，请在 CC-Switch 中手动激活对应通道。",
        "buyUrl": "https://pay.ldxp.cn/shop/Q5L5OORI",
        "downloadLabel": "下载 CC-Switch",
        "downloadFilename": "CC-Switch-v3.14.1-Windows_8.msi",
        "downloadUrl": "",
        "claudeName": "云逸Claude",
        "claudeEndpoint": "https://yunyi.cfd/claude",
        "codexName": "云逸Codex",
        "codexEndpoint": "https://yunyi.cfd/codex",
    }


def get_site_settings() -> Dict[str, Any]:
    saved = get_config("site_settings", {}) or {}
    settings = {**default_site_settings(), **saved}
    filename = str(settings.get("downloadFilename") or "").strip()
    if filename:
        settings["downloadUrl"] = f"/downloads/{filename}"
    return settings


async def save_upload(upload: Optional[UploadFile], subdir: str) -> str:
    if not upload or not upload.filename:
        return ""
    suffix = Path(upload.filename).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        suffix = mimetypes.guess_extension(upload.content_type or "") or ".bin"
    target_dir = UPLOAD_DIR / subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}{suffix}"
    target = target_dir / filename
    with target.open("wb") as fh:
        shutil.copyfileobj(upload.file, fh)
    return f"/uploads/{subdir}/{filename}"


def row_to_resource(conn: sqlite3.Connection, row: sqlite3.Row) -> Dict[str, Any]:
    links = conn.execute(
        "select pan_type, url, code from resource_links where resource_id = ? order by id asc",
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "title": row["title"],
        "type": row["category"],
        "text": row["intro"],
        "image": row["image_path"],
        "source": row["source"],
        "external_id": row["external_id"],
        "clickCount": row["click_count"] if "click_count" in row.keys() else 0,
        "effectiveClickCount": row["link_click_count"] if "link_click_count" in row.keys() else 0,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "panLinks": {link["pan_type"]: link["url"] for link in links},
        "panCodes": {link["pan_type"]: link["code"] for link in links if link["code"]},
    }


def upsert_resource(
    *,
    title: str,
    category: str,
    intro: str,
    image_path: str,
    pan_links: Dict[str, Dict[str, str]],
    source: str = "manual",
    external_id: str = "",
) -> int:
    with db() as conn:
        if source != "manual" and external_id:
            row = conn.execute(
                "select id from resources where source = ? and external_id = ?",
                (source, external_id),
            ).fetchone()
        else:
            row = None

        if row:
            resource_id = row["id"]
            conn.execute(
                """
                update resources
                set title = ?, category = ?, intro = ?, image_path = ?, updated_at = ?
                where id = ?
                """,
                (title, category, intro, image_path, now_sql(), resource_id),
            )
            conn.execute("delete from resource_links where resource_id = ?", (resource_id,))
        else:
            cur = conn.execute(
                """
                insert into resources(title, category, intro, image_path, source, external_id, created_at, updated_at)
                values(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (title, category, intro, image_path, source, external_id, now_sql(), now_sql()),
            )
            resource_id = int(cur.lastrowid)

        for pan_type, payload in pan_links.items():
            url = str(payload.get("url") or "").strip()
            if not url:
                continue
            conn.execute(
                """
                insert into resource_links(resource_id, pan_type, url, code)
                values(?, ?, ?, ?)
                """,
                (resource_id, pan_type, url, str(payload.get("code") or "")),
            )
    return resource_id


def update_resource(
    *,
    resource_id: int,
    title: str,
    category: str,
    intro: str,
    image_path: str,
    pan_links: Dict[str, Dict[str, str]],
) -> int:
    with db() as conn:
        row = conn.execute("select image_path from resources where id = ?", (resource_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="resource not found")
        final_image = image_path or row["image_path"]
        conn.execute(
            """
            update resources
            set title = ?, category = ?, intro = ?, image_path = ?, updated_at = ?
            where id = ?
            """,
            (title, category, intro, final_image, now_sql(), resource_id),
        )
        conn.execute("delete from resource_links where resource_id = ?", (resource_id,))
        for pan_type, payload in pan_links.items():
            url = str(payload.get("url") or "").strip()
            if not url:
                continue
            conn.execute(
                """
                insert into resource_links(resource_id, pan_type, url, code)
                values(?, ?, ?, ?)
                """,
                (resource_id, pan_type, url, str(payload.get("code") or "")),
            )
    return resource_id


def resource_pan_links(
    panLinkQuark: str,
    panLinkBaidu: str,
    panLinkGuangya: str,
    panLinkXunlei: str,
) -> Dict[str, Dict[str, str]]:
    pan_links = {
        "夸克网盘": {"url": panLinkQuark},
        "百度网盘": {"url": panLinkBaidu},
        "光鸭网盘": {"url": panLinkGuangya},
        "迅雷网盘": {"url": panLinkXunlei},
    }
    return {name: payload for name, payload in pan_links.items() if payload["url"].strip()}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(APP_DIR / "static" / "index.html")


@app.get("/admin")
async def admin() -> FileResponse:
    return FileResponse(APP_DIR / "static" / "admin.html")


@app.get("/tutorial")
async def tutorial() -> FileResponse:
    return FileResponse(APP_DIR / "static" / "tutorial.html")


app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")


@app.get("/downloads/{filename}")
async def download_file(filename: str) -> FileResponse:
    target = DOWNLOAD_DIR / filename
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="download not found")
    return FileResponse(target, filename=target.name)


@app.get("/api/site-settings")
async def api_site_settings() -> Dict[str, Any]:
    return {"settings": get_site_settings()}


@app.get("/api/admin/site-settings")
async def api_admin_site_settings(_: str = Depends(require_admin)) -> Dict[str, Any]:
    return {"settings": get_site_settings()}


@app.put("/api/admin/site-settings")
async def api_admin_update_site_settings(payload: Dict[str, Any], _: str = Depends(require_admin)) -> Dict[str, Any]:
    current = get_site_settings()
    updated = {
        **current,
        "brandName": str(payload.get("brandName") or current["brandName"]).strip() or current["brandName"],
        "siteDomain": str(payload.get("siteDomain") or current["siteDomain"]).strip() or current["siteDomain"],
        "announcement": str(payload.get("announcement") or "").strip(),
        "buyUrl": str(payload.get("buyUrl") or "").strip(),
        "downloadLabel": str(payload.get("downloadLabel") or current["downloadLabel"]).strip() or current["downloadLabel"],
        "claudeName": str(payload.get("claudeName") or current["claudeName"]).strip() or current["claudeName"],
        "claudeEndpoint": str(payload.get("claudeEndpoint") or current["claudeEndpoint"]).strip() or current["claudeEndpoint"],
        "codexName": str(payload.get("codexName") or current["codexName"]).strip() or current["codexName"],
        "codexEndpoint": str(payload.get("codexEndpoint") or current["codexEndpoint"]).strip() or current["codexEndpoint"],
        "downloadFilename": current.get("downloadFilename", ""),
        "downloadUrl": current.get("downloadUrl", ""),
    }
    set_config("site_settings", updated)
    return {"settings": get_site_settings()}


@app.post("/api/admin/download-package")
async def api_admin_upload_download_package(
    file: UploadFile = File(...),
    _: str = Depends(require_admin),
) -> Dict[str, Any]:
    if not file.filename:
        raise HTTPException(status_code=400, detail="file required")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".msi", ".zip", ".exe", ".dmg", ".pkg", ".deb", ".rpm", ".appimage"}:
        raise HTTPException(status_code=400, detail="unsupported file type")
    safe_name = f"{uuid.uuid4().hex}{suffix}"
    target = DOWNLOAD_DIR / safe_name
    with target.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)
    settings = get_site_settings()
    settings["downloadFilename"] = safe_name
    set_config("site_settings", settings)
    return {"settings": get_site_settings()}


@app.get("/api/categories")
async def api_categories() -> Dict[str, Any]:
    init_db()
    with db() as conn:
        rows = conn.execute("select name from categories order by sort_order asc, id asc").fetchall()
    return {"items": [row["name"] for row in rows]}


@app.post("/api/categories")
async def api_create_category(payload: Dict[str, str]) -> Dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    with db() as conn:
        max_order = conn.execute("select coalesce(max(sort_order), 0) as n from categories").fetchone()["n"]
        conn.execute(
            "insert or ignore into categories(name, sort_order) values(?, ?)",
            (name, int(max_order) + 1),
        )
    return await api_categories()


@app.delete("/api/categories/{name}")
async def api_delete_category(name: str) -> Dict[str, Any]:
    with db() as conn:
        conn.execute("delete from categories where name = ?", (name,))
    return await api_categories()


@app.get("/api/resources")
async def api_resources(
    category: str = "",
    q: str = "",
    source: str = "",
    limit: int = Query(200, ge=1, le=500),
) -> Dict[str, Any]:
    clauses: List[str] = []
    params: List[Any] = []
    if category:
        clauses.append("category = ?")
        params.append(category)
    if q:
        clauses.append("(title like ? or intro like ? or category like ?)")
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
    if source:
        clauses.append("source = ?")
        params.append(source)
    where = f"where {' and '.join(clauses)}" if clauses else ""
    with db() as conn:
        rows = conn.execute(
            f"select * from resources {where} order by updated_at desc, id desc limit ?",
            (*params, limit),
        ).fetchall()
        items = [row_to_resource(conn, row) for row in rows]
    return {"items": items}


@app.get("/api/resources/stats")
async def api_resource_stats(limit: int = Query(500, ge=1, le=1000)) -> Dict[str, Any]:
    today_start, tomorrow_start = today_range_sql()
    with db() as conn:
        rows = conn.execute(
            """
            select
                resources.*,
                coalesce(sum(case when resource_clicks.click_type = 'view' then 1 else 0 end), 0) as today_click_count,
                coalesce(sum(case when resource_clicks.click_type = 'link' then 1 else 0 end), 0) as today_link_click_count
            from resources
            left join resource_clicks
              on resource_clicks.resource_id = resources.id
             and resource_clicks.created_at >= ?
             and resource_clicks.created_at < ?
            group by resources.id
            order by today_click_count desc, today_link_click_count desc, resources.updated_at desc, resources.id desc
            limit ?
            """,
            (today_start, tomorrow_start, limit),
        ).fetchall()
        items = []
        for row in rows:
            item = row_to_resource(conn, row)
            item["clickCount"] = int(row["today_click_count"] or 0)
            item["effectiveClickCount"] = int(row["today_link_click_count"] or 0)
            items.append(item)
    return {"items": items, "date": today_start[:10], "timezone": "Asia/Shanghai"}


@app.post("/api/resources/{resource_id}/click")
async def api_record_resource_click(resource_id: int, payload: Dict[str, str]) -> Dict[str, Any]:
    click_type = "link" if str(payload.get("type") or "") == "link" else "view"
    with db() as conn:
        row = conn.execute("select id from resources where id = ?", (resource_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="resource not found")
        conn.execute(
            "insert into resource_clicks(resource_id, click_type, created_at) values(?, ?, ?)",
            (resource_id, click_type, now_sql()),
        )
    return {"ok": True}


@app.get("/api/updates/today")
async def api_today_updates(limit: int = Query(5, ge=1, le=20)) -> Dict[str, Any]:
    yesterday = (datetime.now(BEIJING_TZ) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    with db() as conn:
        rows = conn.execute(
            """
            select * from resources
            where source = 'manual'
              and category != '副业'
              and updated_at >= ?
            order by updated_at desc, id desc
            limit ?
            """,
            (yesterday, limit),
        ).fetchall()
        items = [row_to_resource(conn, row) for row in rows]
    return {"items": items}


@app.post("/api/resources")
async def api_create_resource(
    title: str = Form(...),
    type: str = Form(...),
    text: str = Form(...),
    panLinkQuark: str = Form(""),
    panLinkBaidu: str = Form(""),
    panLinkGuangya: str = Form(""),
    panLinkXunlei: str = Form(""),
    image: Optional[UploadFile] = File(None),
) -> Dict[str, Any]:
    pan_links = resource_pan_links(panLinkQuark, panLinkBaidu, panLinkGuangya, panLinkXunlei)
    if not title.strip() or not type.strip() or not pan_links:
        raise HTTPException(status_code=400, detail="title, type and at least one pan link required")
    image_path = await save_upload(image, "resources")
    resource_id = upsert_resource(
        title=title.strip(),
        category=type.strip(),
        intro=text.strip(),
        image_path=image_path,
        pan_links=pan_links,
    )
    with db() as conn:
        row = conn.execute("select * from resources where id = ?", (resource_id,)).fetchone()
        item = row_to_resource(conn, row)
    return {"item": item}


@app.put("/api/resources/{resource_id}")
async def api_update_resource(
    resource_id: int,
    title: str = Form(...),
    type: str = Form(...),
    text: str = Form(...),
    panLinkQuark: str = Form(""),
    panLinkBaidu: str = Form(""),
    panLinkGuangya: str = Form(""),
    panLinkXunlei: str = Form(""),
    image: Optional[UploadFile] = File(None),
) -> Dict[str, Any]:
    pan_links = resource_pan_links(panLinkQuark, panLinkBaidu, panLinkGuangya, panLinkXunlei)
    if not title.strip() or not type.strip() or not pan_links:
        raise HTTPException(status_code=400, detail="title, type and at least one pan link required")
    image_path = await save_upload(image, "resources")
    update_resource(
        resource_id=resource_id,
        title=title.strip(),
        category=type.strip(),
        intro=text.strip(),
        image_path=image_path,
        pan_links=pan_links,
    )
    with db() as conn:
        row = conn.execute("select * from resources where id = ?", (resource_id,)).fetchone()
        item = row_to_resource(conn, row)
    return {"item": item}


@app.delete("/api/resources/{resource_id}")
async def api_delete_resource(resource_id: int) -> Dict[str, Any]:
    with db() as conn:
        conn.execute("delete from resources where id = ?", (resource_id,))
    return {"ok": True}


@app.post("/api/requests")
async def api_create_request(
    email: str = Form(...),
    intro: str = Form(...),
    images: Optional[List[UploadFile]] = File(None),
    image: Optional[UploadFile] = File(None),
) -> Dict[str, Any]:
    if not email.strip() or not intro.strip():
        raise HTTPException(status_code=400, detail="email and intro required")
    uploads: List[UploadFile] = []
    if images:
        uploads.extend(images if isinstance(images, list) else [images])
    if image and image.filename:
        uploads.append(image)
    image_paths = [path for path in [await save_upload(upload, "requests") for upload in uploads] if path]
    image_path = image_paths[0] if image_paths else ""
    with db() as conn:
        cur = conn.execute(
            "insert into requests(email, intro, image_path, created_at) values(?, ?, ?, ?)",
            (email.strip(), intro.strip(), image_path, now_sql()),
        )
        request_id = int(cur.lastrowid)
        for path in image_paths:
            conn.execute(
                "insert into request_images(request_id, image_path, created_at) values(?, ?, ?)",
                (request_id, path, now_sql()),
            )
        row = conn.execute("select * from requests where id = ?", (cur.lastrowid,)).fetchone()
        images_out = [item["image_path"] for item in conn.execute(
            "select image_path from request_images where request_id = ? order by id asc",
            (request_id,),
        ).fetchall()]
    item = dict(row)
    item["images"] = images_out
    return {"item": item}


@app.get("/api/requests")
async def api_requests() -> Dict[str, Any]:
    with db() as conn:
        rows = conn.execute("select * from requests order by id desc").fetchall()
        items = []
        for row in rows:
            images = [
                item["image_path"]
                for item in conn.execute(
                    "select image_path from request_images where request_id = ? order by id asc",
                    (row["id"],),
                ).fetchall()
            ]
            if row["image_path"] and row["image_path"] not in images:
                images.insert(0, row["image_path"])
            items.append(
                {
                    "id": row["id"],
                    "email": row["email"],
                    "intro": row["intro"],
                    "image": images[0] if images else "",
                    "images": images,
                    "createdAt": row["created_at"],
                }
            )
    return {"items": items}


@app.delete("/api/requests/{request_id}")
async def api_delete_request(request_id: int) -> Dict[str, Any]:
    with db() as conn:
        conn.execute("delete from request_images where request_id = ?", (request_id,))
        conn.execute("delete from requests where id = ?", (request_id,))
    return {"ok": True}


@app.delete("/api/requests")
async def api_clear_requests() -> Dict[str, Any]:
    with db() as conn:
        conn.execute("delete from request_images")
        conn.execute("delete from requests")
    return {"ok": True}


def default_sidework_config() -> Dict[str, Any]:
    return {
        "baseUrl": "http://resource-service:8080",
        "clientId": "my-site",
        "firstLimit": 20,
        "limit": 10,
        "mark": True,
        "ensure": True,
        "order": "latest",
        "syncInterval": 300,
    }


@app.get("/api/sidework/config")
async def api_get_sidework_config() -> Dict[str, Any]:
    return {"config": {**default_sidework_config(), **(get_config("sidework_resource_config", {}) or {})}}


@app.put("/api/sidework/config")
async def api_put_sidework_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    config = {
        "baseUrl": str(payload.get("baseUrl") or "http://resource-service:8080").rstrip("/"),
        "clientId": str(payload.get("clientId") or "my-site"),
        "firstLimit": int(payload.get("firstLimit") or 20),
        "limit": int(payload.get("limit") or 10),
        "mark": bool(payload.get("mark", True)),
        "ensure": bool(payload.get("ensure", True)),
        "order": str(payload.get("order") or "latest"),
        "syncInterval": max(60, int(payload.get("syncInterval") or 300)),
    }
    set_config("sidework_resource_config", config)
    return {"config": config}


def service_url(config: Dict[str, Any]) -> str:
    base = str(config.get("baseUrl") or "").rstrip("/")
    client_id = str(config.get("clientId") or "my-site")
    params = httpx.QueryParams(
        {
            "first_limit": str(config.get("firstLimit") or 20),
            "limit": str(config.get("limit") or 10),
            "mark": str(bool(config.get("mark", True))).lower(),
            "ensure": str(bool(config.get("ensure", True))).lower(),
            "order": str(config.get("order") or "latest"),
        }
    )
    return f"{base}/api/clients/{client_id}/pending?{params}"


def join_url(base: str, path: str) -> str:
    if not path:
        return ""
    if path.startswith(("http://", "https://", "/uploads/")):
        return path
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def normalize_sidework_item(item: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    links = {**(item.get("links") or {}), **(item.get("new_links") or {})}
    pan_links: Dict[str, Dict[str, str]] = {}
    for provider, label in (("quark", "夸克网盘"), ("baidu", "百度网盘")):
        payload = links.get(provider) or {}
        url = str(payload.get("url") or "").strip()
        if url:
            pan_links[label] = {"url": url, "code": str(payload.get("code") or "")}
    return {
        "title": str(item.get("title") or "未命名副业资源"),
        "intro": str(item.get("intro") or ""),
        "image": join_url(base_url, str(item.get("image_url") or "")),
        "external_id": str(item.get("id") or ""),
        "pan_links": pan_links,
        "remote_time": str(item.get("updated_at") or item.get("transferred_at") or item.get("created_at") or ""),
    }


async def sync_sidework_resources() -> Dict[str, Any]:
    config = {**default_sidework_config(), **(get_config("sidework_resource_config", {}) or {})}
    url = service_url(config)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        return {"ok": False, "error": str(exc), **(await api_sidework_updates())}

    count = 0
    for item in data.get("items") or []:
        normalized = normalize_sidework_item(item, config["baseUrl"])
        if not normalized["pan_links"]:
            continue
        resource_id = upsert_resource(
            title=normalized["title"],
            category="副业",
            intro=normalized["intro"],
            image_path=normalized["image"],
            pan_links=normalized["pan_links"],
            source="sidework",
            external_id=normalized["external_id"],
        )
        with db() as conn:
            conn.execute(
                "update resources set updated_at = ? where id = ?",
                (now_sql(), resource_id),
            )
        count += 1
    return {"ok": True, "synced": count, "service": data, **(await api_sidework_updates())}


async def sidework_sync_loop() -> None:
    await asyncio.sleep(5)
    while True:
        config = {**default_sidework_config(), **(get_config("sidework_resource_config", {}) or {})}
        try:
            await sync_sidework_resources()
        except Exception:
            pass
        await asyncio.sleep(max(60, int(config.get("syncInterval") or 300)))


@app.post("/api/sidework/sync")
async def api_sync_sidework() -> Dict[str, Any]:
    return await sync_sidework_resources()


@app.get("/api/sidework/updates")
async def api_sidework_updates(limit: int = Query(5, ge=1, le=50)) -> Dict[str, Any]:
    with db() as conn:
        rows = conn.execute(
            """
            select * from resources
            where source = 'sidework'
            order by updated_at desc, id desc
            limit ?
            """,
            (limit,),
        ).fetchall()
        items = [row_to_resource(conn, row) for row in rows]
    return {"items": items}


@app.get("/api/debug/db")
async def api_db_path() -> Dict[str, str]:
    return {"db": str(DB_PATH), "uploads": str(UPLOAD_DIR)}
