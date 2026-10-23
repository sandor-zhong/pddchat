# 店铺账号管理与批量登录复用包

本目录用于把当前项目的以下能力迁移到另一个 PyQt5 项目：

- 店铺账号录入、Excel/CSV 粘贴与读取
- 批量账号登录
- 停止批量登录
- 检查选中店铺的登录状态（最多 3 家并发，首次超时自动复检）

## 建议复制的文件

### 核心文件

| 文件 | 用途 |
| --- | --- |
| `account_import.py` | 粘贴文本、Excel 数据解析、字段归一化、账号生成稳定标识 |
| `account_dialog.py` | “账号录入 / 粘贴”对话框 |
| `password_widgets.py` | 密码隐藏/显示控件 |
| `login.py` | Playwright 账号登录、滑块/短信人工验证等待、可停止逻辑 |
| `execution.py` | 批量队列、停止、登录检查并发与二次复检 |
| `worker.py` | 每家店铺的浏览器线程、独立 profile、登录状态判断 |
| `database.py` | 店铺、加密账号、登录状态和批量任务的 SQLite 持久化 |
| `credentials.py` | Windows DPAPI 密码加密/解密 |
| `models.py` | `Shop`、`Cancelled` 等公共模型 |
| `features.py` | 执行模式名称映射 |

`worker.py` 在原项目中同时承载采集功能，因此还静态导入 `scraper.py`、
`service_status.py`、`violation.py`、`customer_service.py`、`appeal_amount.py` 和
`work_order.py`。压缩包保留这些运行依赖，迁移后可以先直接运行，再按新项目需求把
`CollectorWorker.run()` 中非 `account_login` / `login_check` 的分支删除，以缩小依赖。

## 外部依赖

```text
PyQt5
playwright
openpyxl
Pillow
```

安装后还需要执行：

```powershell
playwright install chromium
```

当前密码加密实现使用 Windows DPAPI，所以数据库不能直接换 Windows 用户或电脑后继续解密。
不要把原项目的 `data/`、SQLite 数据库或 `browser_profiles/` 发给其他人。

## 接入顺序

1. 把压缩包中的 `pdd_collector` 目录复制到目标项目（也可改包名，但要同步修改相对导入）。
2. 创建数据库：`db = Database(Path("data/app.sqlite3"))`。构造函数会初始化表结构。
3. 账号录入：创建 `AccountsDialog(db, parent)`；对话框接受后读取 `dialog.keys`。如果
   `dialog.login_after_save` 为真，立即用这些 keys 启动 `account_login`。
4. 创建一个长期存在的 `ExecutionController(db, data_dir, parent)`，不要把它做成临时变量。
5. 批量登录：`controller.start(keys, mode="account_login", headless=False)`。
6. 停止批量登录：`controller.stop()`。它会设置停止标记；当前浏览器操作退出后不再启动下一家。
7. 检查登录状态：`controller.start(keys, mode="login_check", headless=True)`。
8. 连接 `ExecutionController` 的 `changed`、`log`、`verification`、`finished` 信号更新目标界面。

## 最小调用示例

```python
from pathlib import Path

from pdd_collector.account_dialog import AccountsDialog
from pdd_collector.database import Database
from pdd_collector.execution import ExecutionController

data_dir = Path("data")
db = Database(data_dir / "app.sqlite3")
controller = ExecutionController(db, data_dir, parent=main_window)

dialog = AccountsDialog(db, main_window)
if dialog.exec_() == dialog.Accepted and dialog.login_after_save:
    controller.start(dialog.keys, mode="account_login", headless=False)

# keys 必须是 managed_shops 中已启用店铺的 shop_key
controller.start(keys, mode="account_login", headless=False)
controller.stop()
controller.start(keys, mode="login_check", headless=True)
```

同一个 `ExecutionController` 忙碌时不要再次调用 `start()`。目标 GUI 应先检查
`controller.busy`，并在批量登录过程中只启用“停止批量登录”按钮。

## 原项目 GUI 对应位置

原项目 `gui.py` 中值得对照迁移的方法：

- `import_accounts()`：打开账号录入窗口并衔接保存后登录
- `login_selected_accounts()`：批量账号登录
- `stop_account_login()`：停止批量登录
- `check_selected_logins()`：检查选中店铺登录状态
- `launch()`：校验店铺后调用执行控制器
- `execution_changed()`：按钮状态和进度更新

这些方法依赖原项目整套主窗口控件，因此没有直接复制成独立文件；迁移时按目标项目的
选中行/按钮结构调用上面的公共 API 更稳妥。

## 测试参考

压缩包带有 `test_accounts.py`、`test_worker.py`、`test_management.py`，分别覆盖账号解析与
加密、浏览器登录/停止、管理列表与批量任务。`gui_smoke.py` 可用于查阅原 GUI 按钮调用方式。
