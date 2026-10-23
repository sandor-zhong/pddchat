# pddchat 运营界面

给不写代码的运营人员用的桌面客户端：总览仪表盘、店铺管理（新增/重新登录/停用/删除）、运行控制（启动/停止批量处理）、报表导出。

## 为什么是独立的子项目

主项目（`../`）跑在 Python 3.12 的 venv 上，装了 playwright/numpy/openai 等自动化依赖；这个 GUI 用 PyQt5，锁的是 Python 3.11（**不是** 3.12——PyQt5 5.15.2 官方 wheel 只支持到 3.9，退而求其次用同系列的 5.15.9，仍需要 3.11 才装得上），两边完全独立，互不干扰：

- GUI 进程只 `import db`（主项目里唯一不依赖 playwright/环境变量固化路径的模块，纯标准库实现）来读数据库，不会、也不能 import 任何牵涉浏览器操作的模块（`login.py`/`main.py` 等）。
- 任何"打开浏览器操作某个店铺"的动作，都是 GUI 用 `subprocess` 起主项目 3.12 venv 里的 `python.exe` 跑 `main.py`（`--serve`/`--login-only`），GUI 自己的 Python 解释器从头到尾不碰 playwright。

## 环境准备

首次使用需要先装好 Python 3.11（本仓库用 `pyenv-win` 管理，`gui/.python-version` 已经锁了 `3.11.9`）：

```bash
pyenv install 3.11.9   # 已经装过可以跳过
cd gui
uv sync
```

## 配置

首次运行前复制一份配置：

```bash
cp config.example.json config.json
```

编辑 `config.json`，把 `pddchat_root`/`pddchat_python` 指向主项目根目录和它的 `python.exe`（默认值已经按"gui/ 是主项目仓库内的子目录、主项目用 `.venv`"这个当前布局猜好了，通常不用改；只有把 GUI 打包分发到别的机器时才需要手动改）。

## 运行

```bash
uv run python -m app.main
```
