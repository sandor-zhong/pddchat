# 拼多多商家申诉批量处理工具

## 环境准备

```bash
uv sync
```

首次运行前需在 `state/` 目录下有已登录的浏览器 Profile。

---

## main.py — 全流程流水线

三阶段批处理：采集订单号 → 查聊天记录+AI分析 → 模拟申诉填表。

### 命令一览

| 命令 | 阶段 | 说明 |
|------|------|------|
| `python main.py` | 1+2+3 | 全部三阶段（填完点取消，不提交） |
| `python main.py --confirm` | 1+2+3 | 全部三阶段 + 真实提交申诉 |
| `python main.py --confirm --manual-captcha` | 1+2+3 | 真实提交 + 人工手动滑验证码 |
| `python main.py --collect` | 1 | 仅采集可申诉订单号及问题类型 |
| `python main.py --analyze` | 2 | 仅查聊天记录 + AI 分析（自动读取最新采集文件） |
| `python main.py --submit` | 3 | 仅填表（点取消，不提交） |
| `python main.py --submit --confirm` | 3 | 仅填表 + 真实提交 |
| `python main.py --collect --analyze` | 1+2 | 采集 + 分析 |
| `python main.py --analyze --submit` | 2+3 | 分析 + 填表 |

### 辅助参数

| 参数 | 作用 |
|------|------|
| `--order-file <文件名>` | 指定 output/ 下的 JSON 文件作为订单来源（跳 Phase 1 时用） |
| `--orders <订单号...>` | 直接指定订单号，空格分隔 |
| `--manual-captcha` | 关闭自动破解验证码，等待人工手动滑动 |

### 辅助参数

| 参数 | 说明 |
|------|------|
| `--order-file <文件名>` | 指定 output/ 下的 JSON 文件作为订单来源 |
| `--orders <单号...>` | 直接指定订单号列表，空格分隔 |
| `--manual-captcha` | 关闭自动破解验证码，改由人工手动滑动 |

---

## 调试建议

```bash
# 最安全的调试流程：逐步执行
python main.py --collect                          # Step 1: 只采集订单号
python main.py --analyze                          # Step 2: 查聊天 + AI 分析
python main.py --submit                           # Step 3: 填表但点取消，检查结果

# 确认无误后，指定单个订单号调试验证
python main.py --submit --orders 260805-552620593590168
```

`--submit` + `--orders` 是最安全的调试方式——可反复在同一订单上验证填表逻辑而不产生副作用。

---

## 阶段说明

```
采集阶段 (run_appeal_batch.py / main.py --collect)
  ├── 翻页遍历"可申诉"标签页
  ├── 提取订单号 + 问题类型（商品问题/服务问题）
  └── 写入 output/appealable_orders_<run_id>.json

分析阶段 (main.py --analyze)
  ├── 逐单打开聊天页面，抓取聊天记录
  ├── 无聊天记录的订单截图存证
  ├── 有聊天记录的订单调用大模型分拣申诉凭证类型
  └── 写入 output/chat_<订单号>.json

提交阶段 (main.py --submit)
  ├── 逐单进入申诉详情页
  ├── 自动判定 A/B/C/ALREADY_APPEALED 四种状态
  ├── 按问题类型分支填写不同的申诉表单
  │   ├── 商品问题：选申诉原因 → 填说明 → 上传凭证
  │   └── 服务问题：选"其他" → 填说明 → 上传凭证（渐进渲染）
  └── 默认点"取消"，加 --confirm 则真实提交
```

## 申诉详情页状态对照

| 状态 | 识别条件 | 表单类型 |
|------|----------|----------|
| A | "当前平台未采取实际处理措施" | 模态框：radio + textarea + 图片 |
| B | `detail_confront_new_status` + `<a>` 链接 | 模态框：二次确认 + radio(渐进) + 图片 |
| C | `detail_confront_new_status` + `<button>` | 新页面：select 下拉 + textarea + 图片 |
| ALREADY_APPEALED | 状态文本含"已申诉" | 跳过 |

## 输出文件

```
output/
├── appealable_orders_<run_id>.json   # 采集的订单号及问题类型
├── chat_<订单号>.json                 # 聊天记录 + 分析结果 + issue_type
├── chat_<订单号>_evidence_*.png       # 聊天凭证截图
└── ...
debug/
└── appeal_form_filled_<订单号>.png    # 服务问题填表后截图
```
