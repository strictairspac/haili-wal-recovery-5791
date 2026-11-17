# 海砾 · 采样账本 WAL 恢复演练
海砾计量数据保障组（8人）用可重放日志替代恢复演练时手工拼接的SQL片段和账本截图。

## 场景与模块
上游为事务日志样例，下游为值班复核人。原流程人工标记提交、表格算恢复余额、截图写结论；痛点是崩溃点不清、撤销顺序容易错、恢复证据缺失。模块为事务日志集 / 故障注入点 / 恢复队列 / 恢复证据，实体LogSet/CrashCase/Recovery贯穿全栈。

**Python WAL教学模型，不注入真实PostgreSQL故障，不执行数据库物理恢复。** PostgreSQL真实保存业务输入、冻结快照、任务和结果，Redis实际用于worker互斥，后台Python实际演算恢复历史。

## 启动
```bash
cp .env.example .env
docker compose up -d --build
# http://localhost:18246
# http://localhost:19246/docs
docker compose exec -T backend python -m scripts.seed_demo
docker compose down --remove-orphans
```
SEED_DEMO=true时启动自动执行种子；自然键查重，不覆盖用户修改。初始化失败显式阻止启动，不隐藏结构错误。删除数据需显式down -v。

|账号|密码|权限|典型动作|
|---|---|---|---|
|admin@haili.local|Recovery2026!|读写/导出/审计|查询审计记录|
|operator@haili.local|Recovery2026!|新增日志/场景/运行/导出|设置断电点10|
|viewer@haili.local|Recovery2026!|只读/导出|核对恢复账本|

## 一天的复核剧本
09:00操作员登录；09:05查看海砾采样账本-晚班（A100、B50）；09:10复制日志集保存shift-20261117；09:15注入断电点：执行到第10条，日志落盘到第10条，把未提交的sample_B页写回时刻设为10（越过日志落盘位置的组合会被拒绝）；09:20提交恢复；09:25核对页映像先含未提交B35、撤销后回到B50，A页已含的T1写入不重复重做、T3的提交写入重做为A60，最终A60/B50；09:30下载JSON；09:40观察员核对且写操作403；10:00管理员查看/api/audits。

## 算法、格式和失败态
日志为JSON数组，操作为begin/write/commit/abort/checkpoint；write包含tx/key/value，其他事务操作包含tx，checkpoint不带tx。键必须在初始整数账本存在。事务ID不可复用；严格写锁直到commit/abort释放，同键重叠活动事务拒绝；检查点仅在无活动事务时允许。最多200条日志/30键/整数绝对值不超过10亿。

crash_after=N表示前N条日志已持久化。最近静止检查点保存全部数据页，检查点后数据页假定均未写回（no-force）。恢复重放日志写入和已abort的撤销，再按LSN倒序撤销未结束事务。独立oracle从检查点账本仅应用已提交事务写入，结果须相同。输出重做/撤销轨迹、事务集合、各阶段账本、SHA256和模型版本。空输入、未知键、事务顺序非法、检查点有活动事务、写锁冲突、崩溃点超限均拒绝。模型不涉及页LSN、CLR、模糊检查点或真实磁盘故障；不是完整ARIES实现。

## 场景：执行位置、日志落盘位置与页写回时刻
故障场景除执行位置（executed_through，旧字段crash_after仍兼容）外，可指定日志落盘位置（log_flushed_through）和各键页写回时刻（page_flushes，`{键: LSN}`，0或省略表示未写回）。执行位置之后的日志在崩溃时已执行但不可见；日志落盘位置之后仍未落盘的日志、写入与提交在恢复时丢弃并逐条标注“未落盘丢失”。页写回时刻取值于执行位置以内。

写前规则：数据页（含静止检查点一次性写回全部页）只能在其反映的日志已落盘后写回。任一已执行检查点或页写回LSN越过日志落盘位置，场景保存即422拒绝，不会静默生成磁盘上不可能出现的历史。

恢复以实际落盘页为起点，而非检查点：页LSN不小于日志LSN的写入视为页中已有，跳过重做并记录跳过原因；其余已落盘写入与abort撤销照常重做；随后按LSN倒序撤销无落盘提交的事务。落盘的未提交页值因此被撤销，已提交但未写回的写入被重做，同键多次写入按LSN逐条判定，页值已是撤销目标时记为“撤销无变化”。严格写锁与静止检查点校验不变。

场景保存时即冻结合法页映像（各键页值与页LSN）；入队时快照携带该映像和场景参数，worker只按快照恢复，入队后不再读取可变场景。结果页给出每个键的初始值、页LSN、恢复前后页值、重做/跳过/撤销计数与跳过原因；旧场景（仅crash_after）按no-force语义可继续运行与下载。

## 工程与开发
前端原生HTML/CSS/JS+Nginx1.27；后端Python3.11+FastAPI+SQLAlchemy+PyJWT；PostgreSQL16和Redis7；5服务均healthcheck。持久任务队列在PG，单worker通过Redis锁互斥。20条待执行配额，审计记录写入操作，JWT8小时、PBKDF2随机盐密码。后端路由不含/api，Nginx/api/去前缀代理。

目录frontend/src/{api,stores,types,components/common,hooks,pages,router,utils,constants}与backend/src/{routes,controllers,services,models,schemas,middlewares,utils,constants,config}。WAL引擎与场景规则回归测试在backend/tests/，于backend目录运行`python3 -m unittest discover -s tests`（仅标准库）。本地开发安装backend/requirements.txt并设DATABASE_URL/REDIS_URL/JWT_SECRET，再运行uvicorn src.main:app；前端需代理/api。migrations记录初始化结构，暂无在线升级器。

环境COMPOSE_PROJECT_NAME控制命名，FRONTEND_PORT/BACKEND_PORT/DB_PORT为18246/19246/17746，DB_PASSWORD控制演示数据库密码，JWT_SECRET签名密钥，SEED_DEMO控制种子。命名卷db-data/redis-data/artifacts；共享artifacts预留，导出由冻结快照即时生成。Google Fonts需外网，业务离线可运行。默认凭据仅本地演示。

共享枚举roles与任务states定义于backend/src/constants/enums.py、frontend/src/constants/enums.js，状态使用models/entities.py/services/recovery_service.py/worker.py/components/common/status.js；日志操作类型在schemas/log_set.py和pages/logs.js中声明/说明。MIT许可。真实验证见批次test-artifacts/gb-4107/TESTING.md。
