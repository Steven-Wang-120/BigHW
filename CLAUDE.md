# 项目规矩

> 2026-10-13 最新状态（同日第二段）：用户已明确授权进入施工。已实现 B01 环境与骨架（`pyproject.toml`、`requirements.lock`、`主程序/cli.py`、`repositories/database.py`、`services/health_service.py`、`web/app.py`）与 B03 中不依赖配对策略的规则硬化及自动测试，实测 `python -m pytest 测试` = 141 passed / 1 warning。B02 扫描快照、B04 配对（P-03 待确认）、B05–B11 仍未实现。下文旧“未commit/业务实现仍未开始”为历史阶段状态；实际提交结果以当日 `提交记录.txt` 为准。

当前阶段：施工进行中（B01 已完成、B03 部分完成），2026-10-13。原“只写文档”硬约束已被用户本轮施工授权取代，不再是当前限制；配对策略未确认前不得实施 B04。

## 启动方式（B01 已实现，真实可用）

在项目虚拟环境中按锁定依赖安装后：

- `python -m 主程序.cli init-db` —— 显式初始化 bootstrap 库（幂等、不删库重建、拒绝未知库）；默认路径 `数据/runtime/bootstrap.sqlite3`。
- `python -m 主程序.cli serve --host 127.0.0.1 --port 8000` —— 启动最小服务，仅 `/health` 只读校验 schema 与外键；不自动建库，需先 init-db；只允许回环地址。
- `python -m 主程序.cli prepare-snapshot` —— **未实现**，退出码 2，不扫描任何数据。
- `python -m pytest 测试` —— 已实测 141 passed / 1 warning。

当前仅 bootstrap 层：无业务表、无导入/配对/权限/页面。B02 扫描快照与 B04 配对未实现，配对策略 P-03 待用户确认。不需要 Node/npm 前端构建，不用 React/Vue/Jinja；这不豁免对 node/npm/python/git 及课程 provider 配置的核验。课程工具环境与 MARINA 分开，不打印密钥，不修改 global 配置。

## 目录职责和阅读

先读README→根三规格→文档/研究/文件结构与接口契约→文档/agent/C00至C04→具体文档/施工/B编号。domain纯规则无IO，services编排事务，repositories持久化，web router调用service，静态HTML/JS/CSS同源Fetch。数据raw只官方原件，processed未来真实快照，fixtures只明确受控测试（唯一未来Git白名单小型纯合成controlled_cases.json随测试提交，本轮不创建），runtime本地库；真实大数据继续外置/忽略，文档与交付材料不放行级交易。B/C不是本轮施工授权。普通可逆工作在已授权范围自主做，不凭空要求每文件批准。

## 不许乱改与证据

不改raw zip/xlsx、保真课程原件及已有课程脚本；不运行课程打包/转换/bypass，不修改全局CLI权限、不绕过拒绝。官方仅一流水、年度sheet非两账；配对为推断且保守选择待确认，不能宣称真实原单/准确率。清洗/切片后续另产文件并保留SHA/sheet/原行号，分母不能用dimension冒充扫描。不得造两份官方账、假测试/提交/截图/多日日记/第三条用户选择；未来失败也要记录。用户2项选择：FastAPI+SQLite+服务端网页、原话“前端用HTML,JS和CSS搭就行”，不得虚构用户理由。

保护用户未提交改动；不reset/覆盖/删他人文件；遇既有目标先读取且差异化修改。只在D:\Code\BigHW写项目；文档工具临时脚本放任务临时目录。本轮Git只init未stage/commit；以后施工已授权范围普通本地提交在协调端审阅后自主完成，不逐次另问用户；保护diff、不造历史、不改global identity。推送/外发、不可逆或越范围动作仍须授权并正常审批。秘密在项目外配置，不入文件/日志/历史。目录规则/编号/数据契约变更同步根/B/C/TC/Word，按C02记录，别悄悄删边界。
