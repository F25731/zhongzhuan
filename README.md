# 云逸中转站

这是一个可直接部署的中转站前台，包含：

- 首页一键导入 Claude / Codex 到 CC-Switch
- 独立教程页
- 极简后台
- 公告、购买卡密链接、下载包地址可在线修改
- Docker Compose 部署

## 技术选型

当前使用：

- FastAPI
- SQLite
- 原生静态前端页面
- Docker Compose

这类站点主要是静态内容展示和少量配置读写，不需要为了并发优先切 Go。后续真有流量压力，再拆 Go 服务也来得及。

## 目录

- 前台首页：`/`
- 教程页：`/tutorial`
- 后台：`/fyanxv`
- 站点后端：`site_app/`

## 默认配置

站点默认品牌：

- 品牌名：`云逸`
- 域名文案：`yunyi.hstudy.xyz`
- 购买地址：`https://pay.ldxp.cn/shop/Q5L5OORI`

导入配置默认值：

- Claude：`https://yunyi.cfd/claude`
- Codex：`https://yunyi.cfd/codex`

## 下载包

默认已经接入一个 CC-Switch 安装包到站点下载目录：

`site_app/data/downloads/CC-Switch-v3.14.1-Windows_8.msi`

后续可以在后台直接重新上传新的安装包，前台下载按钮会自动更新。

## 启动

```bash
docker compose up -d --build
```

访问地址：

- `http://服务器IP:8000/`
- `http://服务器IP:8000/tutorial`
- `http://服务器IP:8000/fyanxv`

## 后台账号密码

通过环境变量设置：

```yaml
SITE_ADMIN_USER: fyanxv
SITE_ADMIN_PASSWORD: change-me
```

建议部署前修改。

## Docker Compose 环境变量

当前站点服务使用：

```yaml
SITE_DB_PATH: /app/data/site.db
SITE_UPLOAD_DIR: /app/data/uploads
SITE_DOWNLOAD_DIR: /app/data/downloads
SITE_ENABLE_SIDEWORK_SYNC: "false"
SITE_ADMIN_USER: ${SITE_ADMIN_USER:-fyanxv}
SITE_ADMIN_PASSWORD: ${SITE_ADMIN_PASSWORD:-change-me}
```

## 后台可修改内容

后台支持修改：

- 品牌名
- 域名文案
- 公告
- 购买卡密地址
- 下载按钮文案
- Claude 显示名称和 endpoint
- Codex 显示名称和 endpoint
- 上传新的 CC-Switch 安装包

## 部署建议

1. 先把当前项目推到你自己的 GitHub 仓库。
2. 服务器拉取代码后执行 `docker compose up -d --build`。
3. 用反代把域名指到 `127.0.0.1:8000`。
4. 登录 `/fyanxv` 修改后台账号密码、公告、购买链接和下载包。

## GitHub 仓库

你的目标仓库：

`https://github.com/F25731/zhongzhuan`

当前工作目录原始 remote 不是这个仓库，推送前建议先切到干净仓库或更新 remote 后再推。
