# Shortdrama Studio

本地运行的短剧剪辑工作台，支持素材管理、时间线编辑、字幕校对和视频导出。

- 项目与剧集管理，多文件导入和断点续传
- 视频、音频、字幕编辑，播放、拆分、撤销和自动保存
- 本地对白识别与逐句校对
- 后台渲染与成片下载

## 运行环境

Python 3.11、Node.js 22、npm、FFmpeg。

## 首次安装与启动

在项目根目录执行：

```sh
python3.11 -m venv backend/.venv
backend/.venv/bin/python -m pip install -r backend/requirements.txt
cp .env.example .env
npm --prefix frontend ci
backend/.venv/bin/python scripts/dev.py start
```

已有环境保留原 `.env` 和虚拟环境，直接执行启动命令。工作台地址：<http://127.0.0.1:3031>。

macOS 也可以双击 `启动工作台.command`。查看状态或停止：

```sh
backend/.venv/bin/python scripts/dev.py status
backend/.venv/bin/python scripts/dev.py stop
```

## 开发检查

```sh
cd backend
.venv/bin/python -m pytest
cd ../frontend
npm test
npm run typecheck
npm run lint
npm run build
```

当前适用于本机单用户，单文件上限 100 MiB，单素材最长 10 分钟。AI 素材理解和自动粗剪仍在开发中。数据、账号凭据、方案和手册保存在本地。
