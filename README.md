# 云逸中转站

这是一个可直接部署的云逸中转站前台，包含：

- 首页一键导入 Claude / Codex 到 CC-Switch
- 独立教程页
- 余额查询页
- 极简后台
- 公告、购买卡密链接、下载包地址在线修改
- Docker Compose 部署

## 技术栈

- FastAPI
- SQLite
- 原生静态前端页面
- Docker Compose

## 页面

- 前台首页：`/`
- 教程页：`/tutorial`
- 余额查询页：`/balance`
- 后台：`/fyanxv`

## 默认配置

- 品牌名：`云逸`
- 域名文案：`yunyi.hstudy.xyz`
- 购买地址：`https://pay.ldxp.cn/shop/Q5L5OORI`
- Claude endpoint：`https://yunyi.cfd/claude`
- Codex endpoint：`https://yunyi.cfd/codex`

## 下载包

默认下载包路径：

`site_app/data/downloads/CC-Switch-v3.14.1-Windows_8.msi`

后台可以上传新的 CC-Switch 安装包，前台下载按钮会自动切换到最新文件。

## 启动

```bash
docker compose up -d --build
```

访问地址：

- `http://服务器IP:8000/`
- `http://服务器IP:8000/tutorial`
- `http://服务器IP:8000/balance`
- `http://服务器IP:8000/fyanxv`

## 后台账号密码

通过环境变量设置：

```yaml
SITE_ADMIN_USER: fyanxv
SITE_ADMIN_PASSWORD: change-me
```

部署前建议修改默认密码。

## Docker Compose 环境变量

```yaml
SITE_DB_PATH: /app/data/site.db
SITE_DOWNLOAD_DIR: /app/data/downloads
SITE_ADMIN_USER: ${SITE_ADMIN_USER:-fyanxv}
SITE_ADMIN_PASSWORD: ${SITE_ADMIN_PASSWORD:-change-me}
```

## 后台可修改内容

- 品牌名
- 域名文案
- 公告
- 购买卡密地址
- 下载按钮文案
- Claude 显示名称和 endpoint
- Codex 显示名称和 endpoint
- 余额查询接口
- 模型统计接口
- 24 小时用量接口
- CC-Switch 下载包、Windows/macOS/Linux 分平台当前包记录和历史包管理

## 部署建议

1. 把项目推送到你的 GitHub 仓库。
2. 服务器拉取代码后执行 `docker compose up -d --build`。
3. 用反代把域名指向 `127.0.0.1:8000`。
4. 登录 `/fyanxv` 修改后台密码、公告、购买链接和下载包。
