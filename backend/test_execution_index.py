"""分层执行数据索引测试：策略生效编号、冷存任务归属与核验冻结。

覆盖需求里的关键约束：
1. 同一资产两个策略编号可分别检索到任务，只按资产查询能看到冲突；
2. 历史回执缺策略编号时能定位已证任务，并核对哪些迁移缺来源；
3. 来源不明的任务不能进入核验；
4. 分层策略切换与旧任务核验并发时，已提交的核验不改用新策略的迁移文件。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))

from app import execution_index
from app.services import service_for
from app.store import store

GREEN = "\033[92m"
RED = "\033[91m"
RESET = "\033[0m"

passed = 0
failed = 0

DATA_ASSETS = os.path.join(os.path.dirname(__file__), "app", "data", "assets")
DATA_INDEX = execution_index.INDEX_DIR
_SNAPSHOT = ""


def snapshot_data():
    """测试会写出生效依据/核验回执，先快照数据目录，结束时恢复。"""
    global _SNAPSHOT
    _SNAPSHOT = tempfile.mkdtemp(prefix="exec-index-test-")
    shutil.copytree(DATA_ASSETS, os.path.join(_SNAPSHOT, "assets"))
    if os.path.isdir(DATA_INDEX):
        shutil.copytree(DATA_INDEX, os.path.join(_SNAPSHOT, "index"))


def restore_data():
    global _SNAPSHOT
    if not _SNAPSHOT:
        return
    shutil.rmtree(DATA_ASSETS, ignore_errors=True)
    shutil.copytree(os.path.join(_SNAPSHOT, "assets"), DATA_ASSETS)
    shutil.rmtree(DATA_INDEX, ignore_errors=True)
    if os.path.isdir(os.path.join(_SNAPSHOT, "index")):
        shutil.copytree(os.path.join(_SNAPSHOT, "index"), DATA_INDEX)
    shutil.rmtree(_SNAPSHOT, ignore_errors=True)
    _SNAPSHOT = ""


def check(desc, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  {GREEN}PASS{RESET} {desc}")
        passed += 1
        return True
    print(f"  {RED}FAIL{RESET} {desc} {detail}")
    failed += 1
    return False


def _task(task_no: str) -> dict:
    for row in store.rows("cold_storage"):
        if row.get("任务编号") == task_no:
            return row
    raise AssertionError(f"冷存任务 {task_no} 不存在")


def test_index_links_activation_to_task():
    """索引把策略生效编号、任务和真实执行分区串起来。"""
    print("\n=== 测试: 分层执行数据索引建立来源关联 ===")
    svc = service_for("cold_storage")
    entries = svc.execution_index_rows()

    linked = [row for row in entries if row["来源状态"] == execution_index.SOURCE_LINKED]
    check("索引存在已关联条目", len(linked) >= 2, f"找到 {len(linked)} 条")

    for row in entries:
        if row["来源状态"] != execution_index.SOURCE_LINKED:
            continue
        check(f"{row['任务编号']} 有执行分区", bool(row["执行分区"]))
        check(f"{row['任务编号']} 有生效依据", bool(row["生效依据"]))


def test_two_policy_numbers_same_asset():
    """同一资产两个策略编号分别检索，命中不同任务。"""
    print("\n=== 测试: 同资产两版策略按生效编号分别检索 ===")
    svc = service_for("cold_storage")

    first, first_total = svc.list_candidates(activation_id="TIERACT-0001")
    second, second_total = svc.list_candidates(activation_id="TIERACT-0002")
    check("第一版策略候选已提交核验，不再出现在待核验候选", first_total == 0,
          f"找到 {first_total} 条")

    # 直接按策略编号在任务列表里检索，确认归属不同
    by_policy_1 = [
        row for row in store.rows("cold_storage")
        if row.get("来源策略编号") == "TIER-0101"
    ]
    by_policy_2 = [
        row for row in store.rows("cold_storage")
        if row.get("来源策略编号") == "TIER-0102"
    ]
    check("策略 TIER-0101 命中独立任务", len(by_policy_1) == 1,
          f"找到 {len(by_policy_1)} 条")
    check("策略 TIER-0102 命中独立任务", len(by_policy_2) == 1,
          f"找到 {len(by_policy_2)} 条")
    check("两版策略命中不同任务",
          by_policy_1 and by_policy_2
          and by_policy_1[0]["任务编号"] != by_policy_2[0]["任务编号"])
    check("第二版策略仍在待核验候选", second_total >= 1,
          f"找到 {second_total} 条")
    check("候选带着真实执行分区",
          bool(second and second[0]["执行分区"]))
    check("生效依据按编号独立存档",
          all(
              os.path.exists(execution_index.evidence_path(item))
              for item in execution_index.list_activations()
              if item["策略生效编号"] in ("TIERACT-0001", "TIERACT-0002")
          ))


def test_asset_only_search_shows_ambiguous_receipt():
    """只按资产查询能看到同资产多版本，历史回执缺策略来源。"""
    print("\n=== 测试: 只按资产查询与缺来源回执核对 ===")
    svc = service_for("cold_storage")
    svc.refresh_execution_index()

    asset_rows = [
        row for row in store.rows("cold_storage")
        if row.get("源资产") == "ASSE-0003"
    ]
    check("同资产查到多条任务", len(asset_rows) >= 3,
          f"找到 {len(asset_rows)} 条")

    unknown = [
        row for row in asset_rows
        if row.get("来源状态") == execution_index.SOURCE_UNKNOWN
    ]
    check("历史回执被标为来源不明", len(unknown) >= 1,
          f"找到 {len(unknown)} 条")
    check("来源不明的回执没有策略生效编号",
          all(not row.get("来源策略生效编号") for row in unknown))
    check("来源说明指出同资产多策略冲突",
          any("多个生效策略" in str(row.get("来源说明", "")) for row in unknown))

    # 按已证任务回溯生效依据，缺来源的迁移要能单独列出
    activation, tasks = svc.trace_activation("TIERACT-0001")
    check("按生效编号取到生效依据", activation is not None)
    check("已证任务可回溯", any(t["任务编号"] == "COLD-9001" for t in tasks))
    missing = [t for t in tasks if not t.get("来源策略生效编号")]
    check("已证任务不落在缺来源清单里", not missing, f"缺来源 {missing}")


def test_unknown_source_cannot_be_verified():
    """来源不明的任务不能进入核验。"""
    print("\n=== 测试: 来源不明任务拦截核验 ===")
    svc = service_for("cold_storage")
    svc.refresh_execution_index()

    target = None
    for row in store.rows("cold_storage"):
        if (row.get("源资产") == "ASSE-0003"
                and row.get("来源状态") == execution_index.SOURCE_UNKNOWN):
            target = row
            break
    check("找到来源不明的历史回执", target is not None)

    if target:
        entry, message = svc.submit_verification(target["id"])
        check("核验被拒绝", entry is None, message)
        check("拒绝原因说明来源不明", "来源不明" in message, message)
        check("任务核验状态未被改写",
              target.get("核验状态") != svc.VERIFICATION_MARK)

    candidates, _ = svc.list_candidates()
    check("来源不明任务不进入候选集",
          all(row.get("来源状态") == execution_index.SOURCE_LINKED
              for row in candidates))


def test_concurrent_switch_keeps_frozen_source():
    """策略切换后，已提交核验仍指向原策略与迁移文件。"""
    print("\n=== 测试: 策略切换不改变已提交核验 ===")
    svc = service_for("cold_storage")
    svc.refresh_execution_index()

    pending = None
    for row in store.rows("cold_storage"):
        if (row.get("策略生效编号") == "TIERACT-0002"
                and row.get("核验状态") != svc.VERIFICATION_MARK):
            pending = row
            break
    check("找到待核验的已证任务", pending is not None)
    if not pending:
        return

    entry, message = svc.submit_verification(pending["id"])
    check("已证任务提交核验成功", entry is not None, message)
    frozen_id = pending.get("核验来源策略")
    frozen_file = pending.get("核验来源文件")
    frozen_partition = pending.get("执行分区")
    check("核验冻结来源策略", frozen_id == "TIERACT-0002", str(frozen_id))
    check("核验冻结迁移文件", frozen_file == "activation_TIERACT-0002.json",
          str(frozen_file))

    # 并发场景：同一资产随即切换一版新策略
    tier_svc = service_for("tier_policy")
    next_id = max(int(row.get("id", 0)) for row in store.rows("tier_policy")) + 1
    store.rows("tier_policy").append({
        "id": next_id, "status": "草稿", "pending": True, "abnormal": False,
        "策略编号": "TIER-9901", "目标资产": "ASSE-0003", "当前层级": "HOT",
        "迁移条件": "访问频次<2次/天", "目标层级": "COLD", "冷却天数": "30",
        "访问频次阈值": "2", "策略版本": "v9",
    })
    policy_entry, policy_msg = tier_svc.activate_policy(next_id)
    check("新策略生效", policy_entry is not None, policy_msg)
    new_activation = policy_entry.get("策略生效编号") if policy_entry else ""

    svc.refresh_execution_index()
    check("新策略产生了不同生效编号", new_activation and new_activation != frozen_id)
    check("已提交核验来源未被新策略替换",
          pending.get("核验来源策略") == frozen_id,
          str(pending.get("核验来源策略")))
    check("已提交核验文件未被新策略替换",
          pending.get("核验来源文件") == frozen_file,
          str(pending.get("核验来源文件")))
    check("执行分区仍为原策略分区",
          pending.get("执行分区") == frozen_partition,
          str(pending.get("执行分区")))

    receipt = svc.read_verification_receipt(
        str(pending.get("任务编号", "")), frozen_id or "")
    check("回执仍对应原策略", receipt is not None
          and receipt.get("activation_id") == frozen_id)
    check("回执记录原执行分区", receipt is not None
          and receipt.get("execution_partition") == frozen_partition)


def test_candidate_filters():
    """候选集只放出已关联且未核验的任务，支持资产/生效编号/策略编号过滤。"""
    print("\n=== 测试: 候选集过滤规则 ===")
    svc = service_for("cold_storage")

    candidates, _ = svc.list_candidates()
    check("候选全部为已关联",
          all(row.get("来源状态") == execution_index.SOURCE_LINKED for row in candidates))
    check("候选全部未提交核验",
          all(row.get("核验状态") != svc.VERIFICATION_MARK for row in candidates))

    by_asset, asset_total = svc.list_candidates(asset_id="ASSE-0003")
    check("按资产过滤命中", asset_total >= 1, f"找到 {asset_total} 条")
    check("按资产过滤结果仍带策略编号",
          all(row.get("策略生效编号") for row in by_asset))

    by_policy, policy_total = svc.list_candidates(policy_number="TIER-0102")
    check("按策略编号过滤只命中对应任务",
          policy_total == 0 or all(
              row.get("来源策略编号") == "TIER-0102" for row in by_policy))


if __name__ == "__main__":
    print("=" * 60)
    print("分层执行数据索引测试")
    print("=" * 60)

    snapshot_data()
    try:
        test_index_links_activation_to_task()
        test_two_policy_numbers_same_asset()
        test_asset_only_search_shows_ambiguous_receipt()
        test_unknown_source_cannot_be_verified()
        test_concurrent_switch_keeps_frozen_source()
        test_candidate_filters()
    finally:
        restore_data()

    print(f"\n{'=' * 60}")
    print(f"结果: {GREEN}{passed} 通过{RESET} / {RED}{failed} 失败{RESET} / "
          f"{passed + failed} 总计")
    print("=" * 60)

    if failed > 0:
        sys.exit(1)
