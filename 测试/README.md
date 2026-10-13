# 测试

本轮交付的是测试设计，不是测试实现。48条具体计划用例见[测试矩阵](测试矩阵.md)，状态全部为“未执行”。不把文档检查、Word渲染检查或课程样例算作业务测试。

- `unit/`：未来domain纯规则单元测试，不将领域码硬套HTTP状态。
- `integration/`：未来services、repositories、事务和SQLite约束测试；受控fixture亦用于集成及隔离真双连接并发边界，不限于单元层。
- 未来`数据/fixtures/controlled_cases.json`为唯一Git白名单小型纯合成输入，随测试提交以支持clone/bundle复现，绝不含真实流水；本轮未创建。
- `acceptance/`：未来FR、三条题面规则和网页流程验收。
- `performance/`：未来NFR-01/02/03真实测量记录。
- `evidence/`：真实输出、截图和QA；本轮只允许文档/Word QA，不放假业务结果。

入口文档：

- [手动验收与现场脚本](手动验收与现场脚本.md)
- [性能测量方案](性能测量方案.md)
- [要做什么](../要做什么.md)
- [怎么算做对](../怎么算做对.md)

课程要求的至少10条真实自动化用例、用户亲手复跑、独立测试agent问题清单、核心逻辑code review均待施工；本README不宣称任何通过。
