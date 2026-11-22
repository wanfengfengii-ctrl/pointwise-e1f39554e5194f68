# 薄板弯曲试验原件档案

试验资料室用本工程收录机台原批、验真登记具名副本、处理质控判废、退役实体卷并签领有效原件。复核员凭签单号读回当班实际交付的结果。需要 Python 3.11，使用标准库。

```sh
python3 -B check_project.py
python3 -B archive.py demo
python3 -B archive.py init ./work
```

`check_project.py` 是平台提供的回归入口。演示使用独立测试临时目录，读取随项目附带的两份有限样例。初始化在工作目录建立 `catalog.sqlite3` 和 `volumes.sqlite3`。同一工作目录再次初始化会打开并检查原存储，保留追加的记录、判废、签单和卷端结局。工作目录缺库或初始化中断时会明确报错并保留现场。

## 原批和卷端

原件行接受映射或 `OriginalRow`，字段为 `original_id`、`source` 和 `reading`。`source` 是上游原出处。`reading` 是有符号整数微米或 `None`，布尔、浮点和文本无效。清册及签单的 `position` 是从 0 开始的输入位置。同正文的重复原件沿用首次位置，后续可见位置因此可以有间隔。同一原件号可以在多份清册中被引用，完整正文必须一致。

```python
from archive import Archive, initialize
from copies.volume import VolumeLocation

initialize('work')
with Archive('work') as desk:
    row = {'original_id': 'Q-7', 'source': 'bend/Q/01', 'reading': -7}
    desk.load('Q', [row])
    desk.volumes.provision_volume('VQ', {'001': row})
    desk.register('Q-7', VolumeLocation('VQ', '001'), row)
    signed = desk.get('Q')
    assert desk.history(signed.receipt_id) == signed
```

`load(batch_id, rows)` 返回固定清册。批号已存在时，有序成员、位置及正文都一致才可重投。`register(original_id, location, candidate)` 返回卷位记录，必须同时验真候选参数和独立卷端存放的正文。同一核验卷位可以重投。`provision_volume` 可向完整的本地卷增加新卷位，已存在的卷位正文固定，已毁卷不能重新投入使用。`inspect` 和 `destruction_id` 从卷库独立读取实体状态及已提交的销毁操作号。物理销毁由档案服务的退役路径发起。

## 判废、退役和签领

- `revoke(order_id, original_ids)` 一次提交具名判废单及现存目标，按目标集合比较重投。任一个目标未知会拒绝整单。不同单号可记录针对同一原件的判废事实。
- `retire(request_id, volume_id)` 持久绑定请求及目标。返回 `RetirementRequest`，其 `state` 为 `waiting`、`ready`、`pending` 或 `retired`，分别表示待迁、覆盖通过待复核、待查和已退。`missing` 是待补原件，`evidence` 是核定的逐件卷外卷位，`operation_id` 标识同卷实体操作。待迁时仍可从旧卷领取。销毁前对每个有效件实读验真，并将待查意图和目录限制一并保存。卷库独立提交销毁。回包丢失时以原请求重开查询，确认未生效且覆盖仍成立时可续办，查证不确定时保持待查。
- `get(batch_id)` 返回 `Receipt`。明细按原序保留原件号、原出处、源位置和读数。实体取件先于签单事务，必要时沿原件身份补取。签单提交即发布，与判废提交在同一个业务库内排序。仍有效的原件取不齐时抛出 `DeliveryUnavailable` 且无签单。实测零保留，缺测跳过合计，已知批可签空明细及整数 0。
- `history(receipt_id)` 读取固定签单。后续判废和退役只作用于新交付。

非法输入、身份冲突及请求冲突报告 `ValueError` 或其专用子类。未知批、原件、卷或签单报告 `KeyError`。新退役请求遇到未能确定目标状态时报告 `VolumeStatusUnavailable`，存储不完整报告 `WorkspaceError`。每个工作线程打开自己的 `Archive` 会话，进程间使用 SQLite 事务排序。Python API 提供在目录事务外触发的可选回调，可在初步退役覆盖后以及领取打开后、读某件前、候选取件后安排确定性交错。

样例 P 的源序为 A、X1、B、X2，暖机件 X1、X2 判废前签单合计 23 μm。同批好件 A 和 B 分别为 7 和 11 μm。另批 C 虽也为 11 μm，仍有独立身份。A 已有 V1 副本且 B 待迁时，V0 保持完好，夜班可签 A、B 共 18 μm。B 的真副本登记后可退役 V0，按原号仍可读回判废前的四行签单。独立样例 S 按顺序展示 7、缺测和 11 μm，签单合计 18 μm。
