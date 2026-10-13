# 项目规矩

> 2026-10-13 收工归档阶段：按用户授权进行真实本地文档存档，3张只读文档预览截图已保存；业务施工仍未授权、业务实现仍未开始。下文旧“未commit/无commit”仅为历史阶段状态；本次实际提交结果与时间以当日 `提交记录.txt` 为准。

当前阶段：详细设计准备，2026-10-13。用户本轮硬约束：只写文档/Word产物，不新增业务或测试实现、空代码骨架；源码清单只写Markdown。用户以后明确进入施工后才按B执行，此限制不是永久禁止代码。

## 未来启动方式 当前不可用

未来拟在兼容Python虚拟环境中安装根pyproject.toml与锁定依赖，通过 `python -m 主程序.cli init-db`、`python -m 主程序.cli prepare-snapshot`、`python -m 主程序.cli serve --host 127.0.0.1 --port 8000` 初始化/切片/启动；测试拟 `python -m pytest 测试`。这些文件/命令**尚不存在或未验证，不能现在宣称可用**。不需要Node/npm前端构建，不用React/Vue/Jinja；这不豁免以后B01对node/npm/claude/python/git、终端及课程provider配置的真实核验。课程工具环境与MARINA分开，不打印密钥，本轮不安装或修改global配置。实际命令由B01施工后校准README和锁定版本，不安装文档生成依赖到项目。

## 目录职责和阅读

先读README→根三规格→文档/研究/文件结构与接口契约→文档/agent/C00至C04→具体文档/施工/B编号。domain纯规则无IO，services编排事务，repositories持久化，web router调用service，静态HTML/JS/CSS同源Fetch。数据raw只官方原件，processed未来真实快照，fixtures只明确受控测试（唯一未来Git白名单小型纯合成controlled_cases.json随测试提交，本轮不创建），runtime本地库；真实大数据继续外置/忽略，文档与交付材料不放行级交易。B/C不是本轮施工授权。普通可逆工作在已授权范围自主做，不凭空要求每文件批准。

## 不许乱改与证据

不改raw zip/xlsx、保真课程原件及已有课程脚本；不运行课程打包/转换/bypass，不修改全局CLI权限、不绕过拒绝。官方仅一流水、年度sheet非两账；配对为推断且保守选择待确认，不能宣称真实原单/准确率。清洗/切片后续另产文件并保留SHA/sheet/原行号，分母不能用dimension冒充扫描。不得造两份官方账、假测试/提交/截图/多日日记/第三条用户选择；未来失败也要记录。用户2项选择：FastAPI+SQLite+服务端网页、原话“前端用HTML,JS和CSS搭就行”，不得虚构用户理由。

保护用户未提交改动；不reset/覆盖/删他人文件；遇既有目标先读取且差异化修改。只在D:\Code\BigHW写项目；文档工具临时脚本放任务临时目录。本轮Git只init未stage/commit；以后施工已授权范围普通本地提交在协调端审阅后自主完成，不逐次另问用户；保护diff、不造历史、不改global identity。推送/外发、不可逆或越范围动作仍须授权并正常审批。秘密在项目外配置，不入文件/日志/历史。目录规则/编号/数据契约变更同步根/B/C/TC/Word，按C02记录，别悄悄删边界。
