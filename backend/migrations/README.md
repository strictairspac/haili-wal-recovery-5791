# 初始化结构
SQLAlchemy metadata.create_all首次建users、log_sets、crash_cases、recoveries、audits。log_sets.name、crash_cases.name唯一，关联使用外键，入队冻结日志、场景参数与合法页映像。

## crash_cases 故障场景字段
|字段|含义|
|---|---|
|crash_after|旧字段，等同“执行位置”，仅旧行使用；新行也写入以保持兼容|
|executed_through|崩溃前执行到的日志LSN（NULL表示旧场景，回退crash_after）|
|log_flushed_through|实际落盘的日志LSN（NULL表示旧场景，回退crash_after）|
|page_flushes|JSON `{键: LSN}`，各键页写回时刻；NULL/空表示从未写回|
|page_image|JSON 冻结合法页映像 `{values, page_lsn, checkpoint_lsn, ...}`|

旧库已有数据时，种子脚本用`ALTER TABLE crash_cases ADD COLUMN IF NOT EXISTS`补齐上述四个可空列；不做在线升级器，新库由create_all直接建全列。旧场景四列全NULL，仍按“仅检查点写回全部页”的no-force语义运行，并可导出。写回时刻越过日志落盘位置的场景在保存时拒绝。
