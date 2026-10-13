# VC-R-03 退货对账与差异处理系统

> 2026-10-13 收工归档阶段：按用户授权进行真实本地文档存档，3张只读文档预览截图已保存；业务施工仍未授权、业务实现仍未开始。下文旧“未commit/无commit”仅为历史阶段状态；本次实际提交结果与时间以当日 `提交记录.txt` 为准。

> 2026-10-13 当前验收结论：本次准备与初步设计已由协调端完成验收；v7 Word 10页全部原尺寸PNG目视及协调端独立结构检查通过（exit0、PASS、errors=[]、warnings=[]）。下文旧v4/v5/v6和执行端待审阅记录为历史，不代表当前状态。业务施工/48TC未执行，配对策略仍推荐待确认。

已验收[设计Word](交付材料/设计说明/VC-R-03交付材料与系统设计说明.docx)：50,451字节，SHA256 `D03357389A155C7599E614257921C2D9CB0605DC31683729AC58948112652FB0`；v7 PDF：10页、608,398字节，SHA256 `00E25617B82D1CD6951A3B76B630493F49F6E84F90625716D71C6C472830CE21`。依据：协调端本轮真实逐页目视及独立执行checker结果。

当前阶段：**详细设计准备交付**（2026-10-13）。本轮补齐规格、接口契约、施工计划、测试矩阵、课程材料指南和设计说明 Word；业务程序与测试实现仍未编写，任何规划命令均未运行。

## 先读什么

1. [要做什么](要做什么.md)：FR/CR/NFR/NR 的唯一编号来源。
2. [怎么做](怎么做.md)：模块、数据流、事务、配对边界和施工顺序。
3. [怎么算做对](怎么算做对.md)：验收原则和真实证据边界。
4. [文件结构与接口契约](文档/研究/文件结构与接口契约.md)：未来路径、数据库契约和JSON接口。
5. [C00 必读顺序](文档/agent/C00-必读顺序和阶段.md)：协作规则；再按 B00-B11 施工文档执行。
6. [测试矩阵](测试/测试矩阵.md)：48条具体计划用例，当前全部未执行。
7. [设计说明](交付材料/设计说明/VC-R-03交付材料与系统设计说明.docx)：v7实际10页；DOCX 50,451字节，SHA256 `D03357389A155C7599E614257921C2D9CB0605DC31683729AC58948112652FB0`；2026-10-13T13:01:28.584261+08:00只读结构检查PASS（Title及标题继承链无pBdr、正文≥11pt、内容不变、无稀疏文本页）。v7目视尚未通过，全部图片交协调端逐页审核。48业务TC仍未执行。
8. [修订进度](文档/审阅/修订进度.md)：Markdown完成清单、v4/v5历史与当前v7产物/逐页审核事项。

## 实际目录与规划目录

- `主程序/`：已建目录和 README；未来拟建 `cli.py`、`domain/`、`services/`、`repositories/`、`web/`，当前无业务源码。
- `数据/raw/uci_online_retail_ii/`：已存在官方 zip/xlsx，原件不改；`processed/`未来放真实派生快照，`fixtures/`未来仅放Git白名单小型纯合成`controlled_cases.json`（本轮不创建），`runtime/`未来本地运行库；真实大数据与运行输出继续外置/忽略。
- `测试/`：已建分层目录和 README；未来拟建测试文件，当前无测试实现。
- `文档/施工/`：B00-B11逐步施工计划；`文档/agent/`：C00-C04协作必读文件；`文档/研究/`：外部依据和契约。
- `交付材料/`：设计说明、模板原件和打包指南；不替代课程规定的根固定文件名。
- `参考资料/课程原件/`：保真课程原件与工具，不作为本项目业务实现。
- `每日工作记录/`：只允许真实日期归档；当前仅准备阶段记录，未达课程≥8日。
- `.claude/skills/`：当前没有技能完成证据。

## 已确认设计选择

用户已确认：Python FastAPI + SQLite + 服务端网页；前端为原生HTML、JS、CSS，通过同源Fetch调用JSON接口，不引入React/Vue/Node前端构建器，Jinja也不是必要依赖；“无Node构建”不等于以后B01免核node/npm实际版本。保守唯一候选配对与90天窗口仍是建议。用户已询问配对含义，协调端已解释取消/退货对应既往销售；算法尚未选择，不计第三条已确认决定。

## 当前状态和禁区

- 已有原件和官方raw数据保留，不重下；manifest中的SHA256、实际表头和抽样边界不可静默改写。
- 当前只写文档和设计说明，不新增 `.py/.js/.html/.css/.sql` 业务/测试文件，不创建空代码骨架，不运行课程脚本、不运行bypass、不声称服务或测试已跑通。
- 不制造两套财务/仓库事实数据；年度sheet只是同一流水的两张表，取消单没有真实原单外键。
- 不伪造用户理由、第三条决定、提交、截图、多日日记或测试结果。`选题自查表.md`区分设计计数与实现/实测待验。
- 根 `CLAUDE.md` 记录未来命令但明确尚不可用；Git若仅初始化也不等于已有课程存档。

## 证据入口

- [课程要求核对](文档/研究/课程要求核对.md)
- [官方数据来源](数据/manifests/官方数据来源.md)
- [准备阶段交接](文档/审阅/准备阶段验收交接.md)
- [设计阶段自检](文档/审阅/设计阶段自检.md)
- [课程原件哈希](参考资料/课程原件/保真复制核对.md)
- [提交材料指南](交付材料/打包说明/提交材料指南.md)

后续施工必须先取得用户明确进入施工的授权，再从B00开始；本轮文档是可执行计划，不是实现交付。


## 历史v6分页修订与交接（协调端发现标题蓝线/尾页）

v5经协调端目视发现第4/5/12/14页大面积空白，排版验收未通过，PDF及14张PNG保留为失败历史；v4同样只作历史证据。v6仅去除章节强制开页、段后7pt改5.5pt、行距1.18改1.12，全部文字逐段一致、字体不缩小、表头重复保留。

v6实际10页，docx 50,453字节，SHA256 `79995117092DE09FF6868E3A4F3E537366E6E59547FD7D0F55464731FD5A513C`。2026-10-13T12:54:27.992593+08:00内容保留/字体/自然分页/页密度/全部PNG结构检查PASS；执行端未做目视检查，协调端须打开所有v6 PNG逐页审核。48业务TC仍未执行。

PDF：`D:\Code\BigHW\测试\evidence\docx_render\design-qa-v6.pdf`，606,611字节，SHA256 `5AA66A15B8CC0044D86005D10DE97B69E0405781C57E37F62EC5DB9970CA57A4`。

全部图片：
- `D:\Code\BigHW\测试\evidence\docx_render\v6-page-01.png`
- `D:\Code\BigHW\测试\evidence\docx_render\v6-page-02.png`
- `D:\Code\BigHW\测试\evidence\docx_render\v6-page-03.png`
- `D:\Code\BigHW\测试\evidence\docx_render\v6-page-04.png`
- `D:\Code\BigHW\测试\evidence\docx_render\v6-page-05.png`
- `D:\Code\BigHW\测试\evidence\docx_render\v6-page-06.png`
- `D:\Code\BigHW\测试\evidence\docx_render\v6-page-07.png`
- `D:\Code\BigHW\测试\evidence\docx_render\v6-page-08.png`
- `D:\Code\BigHW\测试\evidence\docx_render\v6-page-09.png`
- `D:\Code\BigHW\测试\evidence\docx_render\v6-page-10.png`

每页PDF抽取字符数（含页码/空白，非汉字字数）；起止章节和详细首末文本见任务temp checker结果：

| 页 | 抽取字符数 | 起始章节 | 结束章节 |
| --- | ---: | --- | --- |
| 1 | 869 | 入口与说明用途 | 一 课程需要交哪些材料 |
| 2 | 820 | 一 课程需要交哪些材料 | 二 唯一流水与两层行数验算 |
| 3 | 896 | 二 唯一流水与两层行数验算 | 三 金额口径与保守配对 |
| 4 | 976 | 三 金额口径与保守配对 | 三 金额口径与保守配对 |
| 5 | 999 | 三 金额口径与保守配对 | 四 模块 数据流与存储 |
| 6 | 1118 | 四 模块 数据流与存储 | 五 原生网页和工单状态 |
| 7 | 1026 | 五 原生网页和工单状态 | 六 测试与性能怎样验 |
| 8 | 973 | 六 测试与性能怎样验 | 七 按什么顺序施工和留证 |
| 9 | 1048 | 七 按什么顺序施工和留证 | 八 设计优势 取舍与来源 |
| 10 | 751 | 八 设计优势 取舍与来源 | 八 设计优势 取舍与来源 |

只读checker：`C:\Users\17088\AppData\Local\Temp\BigHW-design-20261013\check_word_v6_readonly.py`；结果：`C:\Users\17088\AppData\Local\Temp\BigHW-design-20261013\word-v6-structure-result.json`；无9/14页固定断言，不重跑全仓检查，不把结构PASS当目视通过。

本轮失败记录：最初临时批处理因路径转义SyntaxError未执行；首轮自然分页导出11页仍有末页85字，之后适度收紧间距重导。首轮pdfinfo捕获按UTF-8解析中文stderr出现UnicodeDecodeError，PDF和PNG实际仍生成，现用pypdf只读页信息且子进程错误采用替换解码。首轮v6 PDF/11图存任务temp draft-v6-11page-*，旧v5/v4不动。本轮未业务实现/测试、commit、打包或改raw。


## 当前v7格式修订交接

协调端反馈：已逐页检查v6，发现Title继承蓝色横线及85字孤立尾页，故修订为v7。当前文件读到的生成器为5.5pt/1.12、v6末次记录10页；这些与协调端本次所阅11页有版本差异，历史不覆写为新结论。

v7实际10页；DOCX 50,451字节，SHA256 `D03357389A155C7599E614257921C2D9CB0605DC31683729AC58948112652FB0`；2026-10-13T13:01:28.584261+08:00只读结构检查PASS（Title及标题继承链无pBdr、正文≥11pt、内容不变、无稀疏文本页）。v7目视尚未通过，全部图片交协调端逐页审核。48业务TC仍未执行。

PDF：`D:\Code\BigHW\测试\evidence\docx_render\design-qa-v7.pdf`，608,398字节，SHA256 `00E25617B82D1CD6951A3B76B630493F49F6E84F90625716D71C6C472830CE21`。每页PDF抽取字符数：869, 660, 879, 983, 861, 907, 1116, 1028, 1005, 1203。

全部v7 PNG：
- `D:\Code\BigHW\测试\evidence\docx_render\v7-page-01.png`
- `D:\Code\BigHW\测试\evidence\docx_render\v7-page-02.png`
- `D:\Code\BigHW\测试\evidence\docx_render\v7-page-03.png`
- `D:\Code\BigHW\测试\evidence\docx_render\v7-page-04.png`
- `D:\Code\BigHW\测试\evidence\docx_render\v7-page-05.png`
- `D:\Code\BigHW\测试\evidence\docx_render\v7-page-06.png`
- `D:\Code\BigHW\测试\evidence\docx_render\v7-page-07.png`
- `D:\Code\BigHW\测试\evidence\docx_render\v7-page-08.png`
- `D:\Code\BigHW\测试\evidence\docx_render\v7-page-09.png`
- `D:\Code\BigHW\测试\evidence\docx_render\v7-page-10.png`

只读checker：`C:\Users\17088\AppData\Local\Temp\BigHW-design-20261013\check_word_v7_readonly.py`；结果：`C:\Users\17088\AppData\Local\Temp\BigHW-design-20261013\word-v7-structure-result.json`。只修标题边框/段距/表后空段，不修改设计文字、字体、docGrid或业务。v6/v5/v4证据保留。未业务实现/测试/commit/打包/raw改写；v7目视由协调端完成。
