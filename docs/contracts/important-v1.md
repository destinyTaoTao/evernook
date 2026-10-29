# T06 显式重要交代

对应 [#7](https://github.com/destinyTaoTao/evernook/issues/7)。沿用 [本地 Python 接口及身份边界](python-text-v1.md)；不依赖抽取、向量、后台消费者或保险库。T06 握手新增 `important=true`，初始存储为 v2；当前 [T08](capture-v1.md) 升至 v4，外部契约仍为 1。启动在一个事务中兼容补齐旧表，原始消息、回执和任务不重写；拒绝未知存储版本。

## 标记与回取

`mark_important(access, ImportantMark(...))` 要求指定消息 ID、表示 `original/projection`、预期 `content_version`、非空 `scope`、范围和 `expected_text`：

```python
from evernook import ImportantMark

# memory/access/receipt 来自合成原文“甲🙂é乙”的已提交消息。
item = memory.mark_important(access, ImportantMark(
    receipt.message_id, "original", 1, "synthetic-shared", 1, 4, "🙂e\u0301"))
page = memory.read_important(access, item.important_id, limit=2, content_version=1)
assert page.text == "🙂e"
assert (page.start, page.end, page.next_cursor, page.remaining) == (1, 3, 3, 1)
tail = memory.read_important(access, item.important_id, cursor=page.next_cursor, limit=2)
assert tail.text == "\u0301" and tail.next_cursor is None
```

字符位置是表示文本的 Unicode code point `[start,end)`，不是 UTF-16 单元或字素。完整消息标记同时传 `start=None,end=None` 和完整 `expected_text`；空消息/空片段拒绝。来源不存在、版本不同、表示不同、坐标非法、待核对原文不匹配分别报固定错误，不能创建一半的重要状态。

`scope` 是调用端显式提供的适用范围标签，随引用保存；当前不从它推导权限、核心人设或自动执行规则，不自动强制注入所有查询。同一消息/表示/内容版本/范围标签/坐标重复标记返回原 `ImportantItem`（同 ID、创建时间），不同范围标签是独立标记。引用和重要状态是一个可靠提交的记录，底层原始消息保持不可变；不复制原话到第二个真相源，不新建沉淀任务。`status.important_items` 为已提交标记数量。

`read_important` 按 ID 确定性读取。`ImportantPage` 包含引用元数据、逐字文本、原表示的绝对 `start/end`、`next_cursor` 和剩余 code point 数。默认每页 2048，允许 1–4096 code point；末页 `next_cursor=None,remaining=0`。显式游标必须在所标记的 `[start,end)` 中；预算/坐标中的 bool 等错误类型拒绝。内容不会替换为摘要，也不会悄悄截断。code point 边界可能位于组合字素内，客户端拼接或展示时须尊重这个已确认坐标契约。

失败和未知结果均可用同一完整标记请求重试。已确认标记不依赖待处理任务状态；目前没有后台消费者，失败回归为数据库边界注入的任务失败，不代表已运行真实抽取流水线。进程退出测试证明标记提交前无半条引用、回执后能重启逐字回取；不代表物理断电或备份验收。

## 安全边界

两项操作先执行与原文操作相同的 client/token/contract 检查。标记必须与已保存表示一致：关闭保险库后的敏感消息只能标记占位投影，要求 `original` 或提交原始秘密作为待核对片段会被拒绝。`expected_text` 只在内存校验，不存入引用、日志或模型输入；公开对象 repr 隐藏待核对文本与返回原话。范围标签须为非敏感稳定标签，已知凭证信号拒收。

引用绑定消息 ID、表示类型、内容版本和范围，不能把投影坐标用来反查原件。当前只有不可变 v1 原文/投影，没有保险库原件路径。T07 的更正/撤销/抑制覆盖重要回取及重复标记：目标不再可用时报 `not_recallable`，原引用和原件仍保留供获授权归档审查。备份、dsh 显示前屏障及模型能力的未验收边界保持不变。
