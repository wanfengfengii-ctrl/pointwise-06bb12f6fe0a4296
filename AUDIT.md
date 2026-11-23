# 达标工序抽核

原站按 `README.md` 冻结工艺和发行 V1。中心照旧收录 V1 并冻结接线。新增伴随件供同一路由的抽核票使用，原达标工时报告继续办理。

## 抽核口径

一次工序发生是冻结频次表中一次实际转移，含离站的最后一步。由确切开工入口进入、经固定接线首次在 `PASS` 结束的每条有限完整路径，原概率为沿途各步频次概率的乘积。相同 `(source, target, minutes)` 的重复观察按冻结频次合计。路径上的每一个位置都可作为一个带标结果，其质量等于该完整路径的原概率。以所有带标结果的总质量归一。零分钟转移照计。站内转回入口落地工位仍属本次到访。出站后经接线再进站形成另一次到访，即使件号和端口完全相同也逐次列出。报废、站内无出口和永远无法达标的返流均不贡献质量。

完整路径只用来定义概率。中心的实物票只列公开到访并标出其中一次。站端兑换只抽这一次的工序路径与位置。对固定的选中入出口，该段的每个有限地方路径及其中每个位置，按它从该入口首次出站到所选出口的原路径概率归一。两次抽取合起来应与完整带标路径在公开行程及选中私有段上的分布相同。抽核无步数截止，均匀回调正常工作时抽样须几乎必然结束。

## 发行和接收

- `rework.station.issue.issue_audit_companion(revisions, issued, evidence_id) -> bytes` 使用现有 `RevisionStore`、`StationEvidenceStore` 和原 V1 发行号。读取原发行所绑定的冻结修订。相同原发行再次办理得到同一伴随件字节。
- `rework.routing.intake.receive_audit_companion(catalog, payload) -> str` 使用现有 `RoutingCatalog`。先实收对应 V1，再验存伴随件并返回其 `digest`。每个 V1 发行只对应一个实收伴随件，同件重投返回原摘要，冲突拒收。中心核对 `identity` 与 V1 的完整身份，并核对 `v1_sha256` 与实收件的规范编码。

伴随件是 UTF-8 JSON 对象。字段为 `kind`（固定为 `audit-companion`）、`identity`（与 V1 同形）、`v1_sha256`（`encode_evidence` 字节的 SHA-256 小写十六进制）、`rows` 和 `digest`。每行是 `[entry, exit, weighted_steps]`，与实际 V1 证据行按入出口一一对应，按入出口升序排列。`weighted_steps` 用约分分数字符串表示。它是从该入口首次离站到该出口的各有限路径的原概率乘该路径工序数之和，至少等于对应 V1 的出口概率。无出口证据的入口没有行。件内只交公开身份、端口和汇总证据，私有工位、原观察和站内路径留站。摘要检查传递完整性。中心以通过核验的原站来件为输入。

## 签票、领取和兑换

- `rework.analysis.assessment.publish_audit_ticket(catalog, ledger, route_id, start, randbelow) -> dict | None` 使用现有 `RoutingCatalog`、`ReportLedger`、冻结接线号和 `PortRef` 开工入口。每个路由选件须有其确切实收伴随件。只使用中心持有的副本。没有达标路径时返回 `None`，无新票。
- `rework.report.ledger.read_audit_ticket(ledger, ticket_id) -> dict` 按票号读回签发时保存的原字典。读回沿用存值，源站勘误和中心库重开后也可办理。
- `rework.station.issue.redeem_audit_ticket(revisions, issued, evidence_id, ticket, randbelow) -> tuple[tuple[Observation, ...], int]` 仅使用原站现有两个存储对象和票字典。核对所选访问对应的 `evidence_id`、原发行、冻结修订和伴随件摘要。原站可在中心库已关闭时兑换。返回按经过顺序组成的 `Observation` 元组，最后一步抵达票面出口，另返回唯一标记的零基索引。

返回的票字典由 JSON 可传输的值构成。字段为 `ticket_id`（非空字符串）、`route_id`（冻结接线号）、`start`（`[evidence_id, entry]`）、`companions`、`visits`、`selected` 和 `digest`。`companions` 按 `FrozenRoute.pieces` 的次序列全，每项为含 `evidence_id`、`station`、`revision_id`、`digest` 的对象。`visits` 是形如 `[evidence_id, entry, exit]` 的列表，按实际到访次序从开工口列到达标前的最后一站。`selected` 是该列表中的一个零基索引。票号与其余内容同次提交，返回时须已签存。

对两种对象的顶层 `digest`，取去除该字段后的对象，以 `json.dumps` 的 `sort_keys=True, ensure_ascii=False, separators=(',', ':')` 规范编码为 UTF-8，再求 SHA-256 小写十六进制。兑换方校验票摘要、所选次序和本站绑定。

两个抽样入口都接收调用方提供的 `randbelow(n)`，每次传入正整数，返回 `[0, n)` 内的均匀整数。两端各用独立回调，调用次数和顺序由实现决定。输入结构、类型、摘要、端口或身份错配抛 `ValueError`，查无有效编号的记录或缺少确切实收伴随件抛 `KeyError`。回调返回越界值或非整数抛 `ValueError`。签存失败向上抛 SQLite 错误且无新增票，可重新办理。新入口沿用现有存储对象和无待提交事务的调用约定。

## 样例与运行

根目录的 `audit_example.py` 提供零分钟冻结样例及 `ScriptedRandBelow`。它可以直接运行，输出原核账在直送和站内自返时的对照值。

- `alpha_batch()` 一步送乙，`beta_batch()` 一步达标。两张伴随件每条证据的工序加权质量均为 1，抽中甲和乙各为 1/2。
- 换用 `alpha_batch(retry=True)`，甲每次等概率自返或送出，乙仍一步达标。旧报告仍为 `(1, 0, 0)`，甲的工序加权质量变为 2，抽中甲的概率为 2/3。完整带标分布中，甲只执行一步且抽中乙的质量归一后为 1/6。给定票抽中甲，甲执行一步且标在该步的条件概率为 1/4。甲恰执行两步时，“恰执行两步且标第一步”和“恰执行两步且标第二步”的条件概率各为 1/8。
- 保留甲的零分钟自返，换用 `beta_batch(returning=True)`。乙 `qualified` 接达标，`return` 返甲原入口，`discard` 接报废。票抽中第二次甲到访的概率为 1/8。票面行程未返甲的概率为 9/16。
- `branching_alpha()` 等概率沿一步的 `short` 或两步的 `long` 出站。两个出口都接同一个 `beta_batch()` 入口。公开票能区分两个出口。短路且标甲、短路且标乙、长路且标甲、长路且标乙的概率依次为 1/5、1/5、2/5、1/5。在长路且标甲时，回站兑换两个位置等概率。

项目已有 `.pointwise-runtime.json`。保留原回归，让 `verify` 核对抽核概率和票据兑换，沿 `demo` 展示关站签票、回站兑换和重开读回原票。旧 V1 入口、编码和历史报告继续可用。
