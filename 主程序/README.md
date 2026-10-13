# 主程序 详细设计准备 无源码

当前阶段：VC-R-03详细设计准备，已确认Python FastAPI + SQLite + 服务端网页，前端原生HTML/JS/CSS；尚无业务源码或可运行系统。

- `domain/`：未来纯领域规则，无IO。
- `services/`：未来权限、用例与事务编排。
- `repositories/`：未来SQLite持久化。
- `web/static/`：未来静态页面和JS/CSS，通过同源Fetch调用JSON接口。
- `web/templates/`：保留已有空目录，不使用Jinja；无需前端Node/npm构建，但B01施工时仍须核验实际node/npm版本是否存在（不把“无前端构建”当作“未核环境”）。

本轮仅目录README/.gitkeep，不新增实现或空代码骨架；具体拟建源码只见[Markdown契约](../文档/研究/文件结构与接口契约.md)。未来用户明确进入施工后按B01开始，根README的规划命令目前不可用。
