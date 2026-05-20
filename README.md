# 轻量中转分销站

这是一个用于 Claude / Codex 中转服务分销的轻量站点模板，适合有授权上游接口或自有服务接口的团队使用。项目提供前台导入、余额查询、状态展示、教程页和后台配置能力。

## 核心能力

- 首页输入卡密后一键导入 Claude / Codex 到 CC-Switch
- 余额查询页，支持额度型和时长型卡密
- 状态页，展示渠道可用性、成功率和延迟
- 教程页，指导用户安装环境、导入通道、使用 CLI
- 后台配置站点信息、购买地址、接口地址、下载包
- Windows / macOS / Linux 分平台管理 CC-Switch 下载包

## 技术栈

- Python 3.10+
- FastAPI
- SQLite
- 原生 HTML / CSS / JavaScript
- Uvicorn

## 页面入口

- 前台首页：`/`
- 教程页：`/tutorial`
- 余额查询页：`/balance`
- 状态页：`/status`
- 后台：`/fyanxv`

## 目录说明

```text
site_app/
  app.py                  后端服务
  requirements.txt        Python 依赖
  static/                 前端页面和资源
  data/                   SQLite 数据库和下载包目录
docs/
  思路说明.md
  源码使用教程.md
```

## 快速启动

```bash
cd site_app
python -m venv .venv
```

Linux / macOS：

```bash
source .venv/bin/activate
pip install -r requirements.txt
SITE_ADMIN_USER=fyanxv SITE_ADMIN_PASSWORD=change-me uvicorn app:app --host 0.0.0.0 --port 8000
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:SITE_ADMIN_USER="fyanxv"
$env:SITE_ADMIN_PASSWORD="change-me"
uvicorn app:app --host 0.0.0.0 --port 8000
```

访问：

```text
http://服务器IP:8000/
http://服务器IP:8000/fyanxv
```

部署到生产环境时，建议使用 Nginx 反向代理到 `127.0.0.1:8000`，并使用 systemd 或进程管理工具保持服务常驻。

## 后台配置

后台账号密码通过环境变量配置：

```bash
SITE_ADMIN_USER=fyanxv
SITE_ADMIN_PASSWORD=change-me
```

后台可修改：

- 品牌名和域名文案
- 公告和购买卡密地址
- Claude / Codex 显示名称和 API 地址
- 余额查询接口
- 模型统计接口
- 24 小时用量接口
- CC-Switch 下载按钮文案
- Windows / macOS / Linux 安装包

## 接口接入说明

本项目只负责把授权上游或自有服务的接口接入前台，不内置任何第三方账号池。使用前请确认你对上游接口拥有合法授权，并在后台填写对应接口地址。

余额查询接口需要返回卡密状态、套餐信息、额度、用量和到期时间。项目已经兼容常见的额度型和时长型字段，具体字段说明见 `docs/源码使用教程.md`。
