"""VC-R-03 退货对账与差异处理系统 —— 主程序包。

分层约定（见 文档/研究/文件结构与接口契约.md）：
  domain/        纯规则，无 HTTP / 文件 / 数据库 IO
  repositories/  SQLite 持久化，不自行决定配对或工单流转
  services/      权限、用例与事务编排
  web/           FastAPI 路由与静态页；只调用 service，不直接写 SQL

Current scope: B01 bootstrap-only database and health entry point, plus pure
B03 normalization rules. Scanning, snapshot preparation, business import tables,
matching, workflows, authentication, audit, export, and UI are not implemented.
"""

__version__ = "0.1.0"
