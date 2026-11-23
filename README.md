# 精密件返修试验室

白班工艺员从逐次返修观察复核工位规则，冻结修订并发行站件。晚班质控员收录指定站件，冻结出口接线，在两站来源关闭后签发返修核账。次日可以更正工艺、发行新件，并按原号领取昨日结论。报告用于判断所选返修工艺的最终达标机会和达标件累计工时。

## 运行

使用 Python 3.11 标准库及随附的 SQLite。于项目根目录运行 `python3 -B check_project.py` 执行完整回归并串联 AUDIT.md 抽核概率核验，或运行 `python3 -B demo.py` 查看两站返流、次日更正、重开领取以及关站签票、回站兑换、重开读回原票。单独运行 `python3 -B verify_audit.py` 只做抽核概率核验，`python3 -B audit_example.py` 输出零分钟样例的 V1 对照值与伴随件。回归入口由运行环境提供。演示和测试创建独立临时库。可用 `python3 -B -m unittest discover -v` 直接运行同一套测试。

## 白班工艺与站件

- `rework.process.ports.ProcessPorts(workstations, entries, exits)` 使用元组声明工位、`(公开入口, 落地工位)` 对和公开出口。观察目标可为本地工位或公开出口，工位与出口名称须可区分。入口落在已声明工位，站内回到该工位仍是一次本地经过。
- `rework.process.observations.Observation(source, target, minutes)` 记录一次带非负整数分钟的经过。`ObservationBatch(station, ports, observations)` 使用元组保留原始出现顺序和次数。`build_frequencies(batch)` 返回按来源工位的精确复核视图。每个来源的分母为该工位的观察总次数，同去向的不同分钟数分别保留。
- `RevisionStore(connection).initialize()` 建立工艺表。`store.save_draft(batch)` 登记草稿，传同车间的 `draft_id=` 可替换可编辑草稿。`freeze_process(store, draft_id, reviewed_batch=...)` 冻结该草稿，可选参数提交一份经过复核的完整替换批次。每个工位须有出边。冻结后可按 `read_frozen_revision(store, revision_id)` 复核原观察及精确规则。`open_correction(store, old_revision_id, batch)` 创建独立更正草稿。
- `StationEvidenceStore(connection).initialize()` 建立发行表，可与工艺库共享同一个站端连接。`issue_evidence(store, evidence_store, revision_id)` 发行绑定修订的 V1 站件，`read_station_evidence(evidence_store, evidence_id)` 按号重领。调用者通过所持站库连接控制修订的可读范围。

`encode_evidence(piece)` 和 `decode_evidence(payload)` 交换 UTF-8 JSON 字节。V1 信封含 `version`、`identity`、`entries`、`exits` 和 `rows`。`identity` 包含 `evidence_id`、`station` 和 `revision_id`。证据行以公开入口和交接去向索引，后三列为这部分路径的概率、概率加权累计分钟数及概率加权累计分钟数平方。分数采用约分后的字符串，整数也作为字符串。一个入口可以没有交接证据，或只有不足 1 的交接质量。编码及固定站件重领保留公开证据，私有工位与原始观察保存在站端。

## 晚班接线与签发

- `RoutingCatalog(connection).initialize()` 建立中心收件和接线表。`receive_evidence(catalog, payload)` 校验并保留 V1 副本。同号等内容重收返回原固定身份，同号异内容拒绝。`get_received_evidence(catalog, evidence_id)` 重新校验指定存件。
- `PortRef(evidence_id, port)` 定位固定件上的端口。`RouteAssignment(exit, target)` 将出口接到另一个所选入口，或接到 `TerminalDisposition.PASS` / `TerminalDisposition.SCRAP`。`freeze_route(catalog, evidence_ids, assignments)` 固定选件及完整接线。每个所选出口须有唯一映射。成功返回 `FrozenRoute`，之后以 `read_frozen_route(catalog, route_id)` 按号读取。
- `ReportLedger(connection).initialize()` 建立中心报告与领取表，可与中心目录共享连接。`publish_assessment(catalog, ledger, route_id, start)` 按冻结路由和 `PortRef` 开工入口返回已提交的 `ReportSnapshot`。路径可返流多次，只有有限步内抵达达标终点才算达标。报废和无限返修保留其非达标质量。每段工时计一次，均值和方差只对达标路径计算。概率、分钟均值及平方分钟方差都是 `Fraction`。零达标时均值与方差为 `None`。

确定性接力耗时依次为 1 和 2 分钟，应报 1、3、0。两站返流样例中，甲耗时 1 分钟进入乙，乙四次观察分别为达标 2 分钟、达标 2 分钟、返甲 0 分钟、报废 0 分钟。报告应为 2/3、10/3、4/9。达标条件下先返甲的次数服从公比 1/4 的几何权重。将乙的达标工时更正为 5 分钟后，新报告应为 2/3、19/3、4/9。

## 达标工序抽核伴随件与票据

- `rework.station.issue.issue_audit_companion(revisions, issued, evidence_id)` 读取原 V1 发行绑定的冻结修订，返回确定性的 UTF-8 JSON 伴随件字节；同一发行重复办理得到相同字节。伴随件只含公开身份、端口、`v1_sha256`、按入出口排序的工序加权质量行和规范 `digest`，私有工位、原始观察和站内路径不出站。
- `rework.routing.audit_intake.receive_audit_companion(catalog, payload)` 要求对应 V1 已先实收，核验完整身份、V1 规范编码摘要和逐行对应关系后保存，返回伴随件摘要；同件重投返回原摘要，冲突拒收。每个 V1 发行只对应一个实收伴随件。
- `rework.analysis.assessment.publish_audit_ticket(catalog, ledger, route_id, start, randbelow)` 在路由每个选件都有确切实收伴随件时，按“每条有限达标路径上每个工序位置与该路径原概率成正比”的口径签票。两端各用独立的 `randbelow(n)` 均匀回调。无达标路径返回 `None` 且不产生新票。票据含 `ticket_id`、`route_id`、`start`、按 `FrozenRoute.pieces` 排序的 `companions`、有序 `visits`、零基 `selected` 和顶层 `digest`，签存后返回。
- `rework.report.ledger.read_audit_ticket(ledger, ticket_id)` 按票号读回签发时保存的原字典；勘误和中心库重开后仍可办理。
- `rework.station.issue.redeem_audit_ticket(revisions, issued, evidence_id, ticket, randbelow)` 仅用原站两个存储对象，核对票摘要、所选次序、V1 发行、冻结修订和伴随件摘要后，按所选入出口的带标条件分布复演该次到访，返回有序 `Observation` 元组和唯一零基标记位置。原站可在中心关闭后兑换。

`rework.analysis.sampling.categorical` 用分数的最小公倍数精确放大权重后调用回调，回调返回越界值或非整数抛 `ValueError`。结构、类型、摘要、端口或身份错配抛 `ValueError`；查无有效编号或缺少确切实收伴随件抛 `KeyError`。签存失败向上抛 SQLite 错误且无新增票，可重新办理。`python3 -B verify_audit.py` 以 AUDIT.md 样例核验全部抽核概率，`check_project.py` 在单元回归后串联同一核验。

## 重开和失败语义

`read_report(ledger, report_id)` 按原号取回原数值、单位、路由、开工入口和全部固定件出处，并记录本次成功领取。`export_report(snapshot)` 返回带换行的 UTF-8 JSON 字节。报告读取使用已存快照，同一报告的导出在后续更正与重开后保持一致。

未知记录抛出 `KeyError`。输入、冻结状态、身份冲突及损坏证据错误抛出 `ValueError`。SQLite 错误由调用方接收。各存储对象借用调用方提供的连接，写入操作管理自己的事务，须在该连接没有待提交事务时调用。非法批次、发行、接线、核账及失败的报告提交均无对应的新增成果。未知报告号不增加领取记录。可重复调用 `initialize()`，已有草稿及中心业务记录会保留。
