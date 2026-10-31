# 三类申诉提示词离线测试

在项目目录打开 PowerShell：

```powershell
cd D:\PythonProjects\pddchat
```

三个脚本只读 `state/pddchat.db`，调用对应的正式生成函数，并自动导出新旧话术对比 Excel。不打开浏览器、不操作鼠标、不提交申诉，也不改数据库或正式提示词。需要项目 `.venv`、`.env` 中的 `DEEPSEEK_API_KEY` 和 Codex 内置 Excel 运行时。真实生成会消耗该接口的额度。

## 默认测试当前项目提示词

负向申诉，20 单或 50 单：

```powershell
.\.venv\Scripts\python.exe test_negative_prompt.py --count 20
.\.venv\Scripts\python.exe test_negative_prompt.py --count 50
```

复议，20 单或 50 单：

```powershell
.\.venv\Scripts\python.exe test_reconsideration_prompt.py --count 20
.\.venv\Scripts\python.exe test_reconsideration_prompt.py --count 50
```

售后申诉，20 单或 50 单：

```powershell
.\.venv\Scripts\python.exe test_aftersale_prompt.py --count 20
.\.venv\Scripts\python.exe test_aftersale_prompt.py --count 50
```

每次重新执行都会新建运行目录并重新生成，不复用上轮输出、不覆盖旧文件。按保存时间倒序选择有旧话术的记录，保留原状态；有旧文不代表已提交或申诉成功。

## 先看材料数量，不调用模型

```powershell
.\.venv\Scripts\python.exe test_negative_prompt.py --list-only
.\.venv\Scripts\python.exe test_reconsideration_prompt.py --list-only
.\.venv\Scripts\python.exe test_aftersale_prompt.py --list-only
.\.venv\Scripts\python.exe test_aftersale_prompt.py --list-only --include-no-chat
```

截至本次检查，有旧文且有有效买家聊天的记录：负向 232 单、复议 87 单、售后 17 单。数量会随业务数据库变化。

如果目标为 20 或 50 单、可用记录不足，默认在调用接口前停止。可以明确选择以下一种做法：

```powershell
# 只测试实际可用的售后记录，不强行补足20单
.\.venv\Scripts\python.exe test_aftersale_prompt.py --count 20 --allow-fewer

# 纳入真正缺聊天的历史记录，走正式固定兜底；本地目前可组成17单模型生成+3单兜底
.\.venv\Scripts\python.exe test_aftersale_prompt.py --count 20 --include-no-chat

# 目标50单，但按实际可用数量运行，Excel明确记录目标数与实际数
.\.venv\Scripts\python.exe test_aftersale_prompt.py --count 50 --allow-fewer
```

无聊天兜底不算模型生成。缺旧文、缺实际原因、聊天内订单号冲突、明显商品冲突及无有效买家发言的记录不会拿来凑数。复议使用同店铺同订单的 `orders` 聊天；售后优先自己的聊天，缺失时才尝试同店铺同订单的 `orders` 存档。

## 临时测试另一份提示词

将完整的初始系统提示词保存为 UTF-8 文本，例如 `D:\PythonProjects\pddchat\prompts\aftersale_candidate.txt`，然后执行：

```powershell
.\.venv\Scripts\python.exe test_aftersale_prompt.py --count 20 --include-no-chat --prompt-file .\prompts\aftersale_candidate.txt
.\.venv\Scripts\python.exe test_reconsideration_prompt.py --count 20 --prompt-file .\prompts\reconsideration_candidate.txt
.\.venv\Scripts\python.exe test_negative_prompt.py --count 20 --prompt-file .\prompts\negative_candidate.txt
```

这些文件需自行创建并填入文本。`--prompt-file` 只替换本次进程中的初始 system 提示词，不写回正式源码，也不自动拼接原提示词。正式函数自己的调用参数、去重附加上下文和字数处理仍然保留。

不加此参数时，读取以下当前正式提示词：

| 脚本 | 提示词 | 正式生成函数 |
| --- | --- | --- |
| `test_negative_prompt.py` | `appeal_classifier.APPEAL_TEXT_SYSTEM_PROMPT` | `generate_appeal_text()` |
| `test_reconsideration_prompt.py` | `appeal_wording.RECONSIDERATION_DESCRIPTION_PROMPT` | `generate_reconsideration_description()` |
| `test_aftersale_prompt.py` | `aftersale_appeal.AFTERSALE_DESCRIPTION_PROMPT`，来源为 `appeal_wording.py` | `generate_aftersale_description()` |

## 输出位置与内容

默认输出到项目下：

```text
outputs\prompt_tests\<业务类型>_<北京时间>_<随机编号>\
```

每个目录包含：

- `话术对比.xlsx`：历史话术、本次实际输出、实际原因、生成状态、自动提示、字数与调用次数；另含原始聊天和提示词参数页。
- `prompt.txt`：本轮实际初始系统提示词。
- `run.json`：订单与原始聊天快照、原话术、参数、每次模型候选原文、token使用记录、最终函数返回值及代码哈希，可供追溯和续跑。
- 预览 PNG：仅用于检查布局，预览异常不改写已导出的 Excel。

生成失败会独立显示，不会用旧话术或人工改写结果顶替。自动提示主要检查截断、多段输出、图片标记和归属等线索，不判断买家责任、图片有效性或商家是否无责。图片内容本身没有被本测试识别。

负向通常按正式函数的120字逻辑截断，复议超过200字、售后超过300字会被正式函数拒绝；修改提示词不等于修改这些程序限制。脚本不重新判分类、不编造未保存的复议或售后原因。

## 中断后续跑

将命令中的目录换成控制台打印的真实运行目录：

```powershell
.\.venv\Scripts\python.exe test_aftersale_prompt.py --resume "D:\PythonProjects\pddchat\outputs\prompt_tests\aftersale_实际时间_实际编号"
```

成功和兜底记录会跳过，只重试失败或尚未生成的记录。认证、权限或额度异常时停止后续调用，修复后可以续跑。不要换脚本类型；正式代码或测试提示词变化时会拒绝混用旧断点，需新建测试。

## 不调用模型，仅重新导出

```powershell
.\.venv\Scripts\python.exe test_aftersale_prompt.py --export-only "D:\PythonProjects\pddchat\outputs\prompt_tests\aftersale_实际时间_实际编号"
```

负向或复议的运行目录请使用对应脚本。此操作读取 `run.json`，不会再次调用接口或花费模型额度。

## 其他参数

```powershell
# 仅指定店铺：名称必须与数据库一致
.\.venv\Scripts\python.exe test_negative_prompt.py --count 20 --shop "完整店铺名"

# 指定其他只读源数据库及输出父目录
.\.venv\Scripts\python.exe test_negative_prompt.py --count 20 --db .\state\pddchat.db --output-root .\outputs\my_prompt_tests

# 缺少自动检测的运行时时，指定Codex dependencies文件夹
.\.venv\Scripts\python.exe test_negative_prompt.py --count 20 --runtime-root "C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies"

.\.venv\Scripts\python.exe test_negative_prompt.py --help
```

脚本先检查可用数量和 Excel 运行时，再开始调用模型。不要在 Excel 中占用输出文件后重新导出；若文件被占用，关闭 Excel 再用 `--export-only` 即可，已有模型结果仍在 `run.json`。
