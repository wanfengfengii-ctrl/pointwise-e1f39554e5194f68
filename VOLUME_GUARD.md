# 实物核退交接

## 办理入口

- `LocalVolumeStore.provision_volume(volume_id, slots, *, inventory_guard=False)` 沿用卷位名到原件行的映射。新卷第一次建卷时传 `inventory_guard=True` 启用核退。模式随卷固定。省略该参数仍可向已启用的卷追加，显式 `False` 同样保留原模式。对既有普通卷传 `True` 报 `ValueError`。原槽同正文可重投，冲突依旧整次拒绝。
- `Archive.retire(request_id, volume_id)` 办理两类卷，退役仍通过 `Archive(..., port=...)` 所选卷端。沿用同号同卷续办、同号异卷拒绝和同卷实物只销毁一次的行为。核退对外沿用 `RetirementRequest` 的原字段，增加 `unverified_slots` 和 `inventory` 两个元组字段，普通卷两字段均为空。
- 卷端进入实体销毁的调用仍经过 `destroy(volume_id, operation_id, ...)`，前两参数含义沿用原接口。可以扩充授权参数和卷端交接接口。完好的核退卷收到旧式未授权调用时须保留原卷。`inspect` 和 `destruction_id` 继续提供卷端实际结局。

## 盘点与结清

资源归属与正常业务见 README.md。卷端可以先存实物，资料员随后登记。核退检查目标卷全部实存槽，包括未登记的槽。逐槽按 `original_id`、`source`、`reading` 核对收录正文。来源未知、正文不符或槽内原件无法读取时，该槽拦退。核真的判废件仍属于实存槽，免补卷外副本。其余原件按身份核定，每个不同原件须有一个已登记、仍可用于覆盖且本次实读验真的卷外卷位。多个槽可对应同一个原件，证据按原件记，销毁范围按槽位记。

目录与卷端各自提交。机台取得追加成功时，该槽必须进入之后的销毁核定。销毁先成立时，追加报 `ValueError`。核定导致机台写入或退役须稍后重试时可报 `copies.volume.VolumeBusy`，本次追加须保持原子。已确定未销毁且发现缺口时，返回 `waiting`。其中 `missing` 列需补卷外保障的原件号，`unverified_slots` 列无法核对的槽名。两类缺口分别列出，实物仍在，补件或补齐收录后可按原号办理。

销毁答复丢失且实际结局不明时返回 `pending`。已确认此前没有销毁且旧指令不会再生效时，同号可重核续办。`retired` 结果的 `operation_id` 必须与卷端实记的销毁操作一致，`inventory` 为实际被销毁的 `(槽名, 原件号)` 对按槽名排序组成的元组，`evidence` 沿用 `(原件号, VolumeLocation)` 对，记录该次使用的卷外卷位。重开后复领当次结果。卷端已提交而目录结清尚未提交时中断，仍须找回此次实际采用的依据。后续副本与判废变化由后续业务使用，已退结果及历史签单按原记录领取。

## 样例与核验

`guard_scenario.py` 的 `prepare` 放置 GUARD-T/0 的 GUARD-A（7 μm），登记 GUARD-W/0 的 A 副本及 GUARD-W/2 的 GUARD-C（11 μm），另外收录 GUARD-B（11 μm）。它返回 7 μm 的旧签单。通过 `provision_target` 可按本题入口启用目标卷。将 B 追加至 GUARD-T/1 且先不登记，原号应返回待迁，缺保障原件为 B。C 无法替代 B。向 GUARD-W/1 写入并登记 B 的真副本后，同号可销毁并复领 `(('0', 'GUARD-A'), ('1', 'GUARD-B'))` 及 W 上两个对应卷位。按旧签单号仍读得原行和合计 7 μm。

夹具 `ScriptedVolumeStore` 可在 `destroy` 入口执行一次 `before_destroy` 回调，机台追加可从另一卷端会话发起。它还能模拟派发前失答、卷端提交后失答，以及卷端提交后打断目录办理。确认追加成功时 T 应保留至 B 获得真副本。若先封住核定范围，可让机台收到可重试的忙，确认本次未落槽。若一次失答后旧卷外依据已经退出覆盖、第二次改用另一真副本，则卷端实际销毁后即使目录办理中断，重开也应取回实际采用的依据。

初始夹具可用 `python3 -B guard_scenario.py` 检查普通卷的两处失答和重开行为。完成后在现有 `.pointwise-runtime.json` 接通新业务。沿用 `version: 1`、`kind: cli` 和 `setup` 的命令数组列表，`verify` 与 `demo` 仍各为一条 argv。核验运行完整原回归及实物核退联动用例，失败以非零退出。演示从样例的 B 待迁走到补齐、实体退役和重开复领，并展示旧签单。
