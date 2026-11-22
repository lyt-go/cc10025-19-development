"""分层执行数据索引：把策略生效编号、冷存任务和真实执行分区按来源串起来。

同一个资产可以多次切换分层策略，而冷存记录原先只能按资产检索，无法区分是
哪一版策略真正触发了迁移。这里维护一份「策略生效依据」与「分层执行数据索引」：

* 每次 ``tier_policy`` 生效都会生成唯一的 **策略生效编号**，并落一份生效依据文件；
* 冷存任务创建时写入 ``策略生效编号`` / ``来源策略编号`` / ``来源策略版本`` /
  ``执行分区``，索引即可定位任务真正落在哪个分区；
* 历史回执若没有保留策略编号，只有在生效依据唯一可对照时才回填来源，
  同资产存在多个生效策略时一律标记 ``来源不明``，不能靠资产号猜归属；
* 已提交核验的任务冻结 ``核验来源策略``，索引重建也不会改写它的归属。

索引本身只处理普通字典，不依赖任何领域服务，避免和 tier_policy / cold_storage
形成循环导入。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

INDEX_DIR = os.path.join(os.path.dirname(__file__), "data", "index")
ACTIVATION_FILE = os.path.join(INDEX_DIR, "tier_policy_activations.json")
INDEX_FILE = os.path.join(INDEX_DIR, "tier_execution_index.json")
EVIDENCE_DIR = os.path.join(os.path.dirname(__file__), "data", "assets")

SOURCE_LINKED = "已关联"
SOURCE_UNKNOWN = "来源不明"
VERIFIED = "已提交"

_LOCK = threading.RLock()
_ACTIVATIONS: list[dict[str, Any]] = []
_SEQ = 0
_BOOTSTRAPPED = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_dir() -> None:
    os.makedirs(INDEX_DIR, exist_ok=True)


def _write_json(path: str, data: Any) -> None:
    _ensure_dir()
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, default=str)


def _evidence_filename(activation_id: str) -> str:
    return f"activation_{activation_id}.json"


def evidence_filename(activation_id: str) -> str:
    """生效依据的存档文件名，与策略生效编号一一对应。"""
    return _evidence_filename(activation_id)


def _sync_activation_file(record: dict[str, Any]) -> None:
    """生效依据必须和编号一一对应，重放/回填时不能覆盖成别的策略。"""
    _write_json(
        os.path.join(INDEX_DIR, _evidence_filename(record["策略生效编号"])),
        record,
    )
    _write_json(ACTIVATION_FILE, _ACTIVATIONS)


def bootstrap(seed_records: Iterable[dict[str, Any]] | None = None) -> None:
    """把预置的生效依据装载进进程内索引，只执行一次。"""
    global _SEQ, _BOOTSTRAPPED
    with _LOCK:
        if _BOOTSTRAPPED:
            return
        _BOOTSTRAPPED = True
        _ensure_dir()
        if seed_records is None:
            from app.seed import SEED_ACTIVATIONS

            seed_records = SEED_ACTIVATIONS
        seeds = list(seed_records or [])

        # 清掉上次进程遗留、但不在种子里的生效依据，保证启动状态可复现
        seed_ids = {
            str(seed.get("策略生效编号") or "") for seed in seeds
        }
        for name in os.listdir(INDEX_DIR):
            if not (name.startswith("activation_") and name.endswith(".json")):
                continue
            if name[len("activation_"):-len(".json")] not in seed_ids:
                os.remove(os.path.join(INDEX_DIR, name))

        for seed in seeds:
            _SEQ += 1
            record = {
                "策略生效编号": str(seed.get("策略生效编号") or f"TIERACT-{_SEQ:04d}"),
                "资产编号": str(seed.get("资产编号") or ""),
                "策略编号": str(seed.get("策略编号") or ""),
                "策略版本": str(seed.get("策略版本") or ""),
                "当前层级": str(seed.get("当前层级") or ""),
                "目标层级": str(seed.get("目标层级") or ""),
                "执行分区": str(seed.get("执行分区") or ""),
                "生效时间": str(seed.get("生效时间") or _now()),
                "生效依据": _evidence_filename(str(seed.get("策略生效编号") or "")),
                "生效依据文件": _evidence_filename(str(seed.get("策略生效编号") or "")),
            }
            # 允许种子显式给出编号，编号里的序号同样纳入自增计数
            suffix = record["策略生效编号"].rsplit("-", 1)[-1]
            if suffix.isdigit():
                _SEQ = max(_SEQ, int(suffix))
            _ACTIVATIONS.append(record)
            _sync_activation_file(record)
        _write_json(ACTIVATION_FILE, _ACTIVATIONS)


def reset_index(seed_records: Iterable[dict[str, Any]] | None = None) -> None:
    """清空进程内索引并重新装载种子（测试用）。"""
    global _SEQ, _BOOTSTRAPPED
    with _LOCK:
        _ACTIVATIONS.clear()
        _SEQ = 0
        _BOOTSTRAPPED = False
        _write_json(ACTIVATION_FILE, [])
        _write_json(INDEX_FILE, {"generated_at": _now(), "entries": []})
    bootstrap(seed_records)

def allocate_activation_id() -> str:
    """分配全局唯一的策略生效编号。"""
    global _SEQ
    with _LOCK:
        _SEQ += 1
        return f"TIERACT-{_SEQ:04d}"


def record_activation(
    *,
    asset_id: str,
    policy_number: str,
    policy_version: str,
    current_tier: str,
    target_tier: str,
    execution_partition: str,
    activation_id: str | None = None,
    activated_at: str | None = None,
) -> dict[str, Any]:
    """登记一次策略生效，作为冷存任务归属的生效依据。"""
    global _SEQ
    with _LOCK:
        if not activation_id:
            _SEQ += 1
            activation_id = f"TIERACT-{_SEQ:04d}"
        record = {
            "策略生效编号": activation_id,
            "资产编号": str(asset_id),
            "策略编号": str(policy_number),
            "策略版本": str(policy_version),
            "当前层级": str(current_tier),
            "目标层级": str(target_tier),
            "执行分区": str(execution_partition),
            "生效时间": activated_at or _now(),
            "生效依据": _evidence_filename(activation_id),
            "生效依据文件": _evidence_filename(activation_id),
        }
        _ACTIVATIONS.append(record)
        _sync_activation_file(record)
        return dict(record)


def list_activations(
    *,
    asset_id: str | None = None,
    activation_id: str | None = None,
    policy_number: str | None = None,
    policy_version: str | None = None,
) -> list[dict[str, Any]]:
    """按条件读取生效依据，返回副本，避免调用方误改索引。"""
    with _LOCK:
        records = [dict(item) for item in _ACTIVATIONS]
    if asset_id:
        records = [item for item in records if item.get("资产编号") == asset_id]
    if activation_id:
        records = [item for item in records if item.get("策略生效编号") == activation_id]
    if policy_number:
        records = [item for item in records if item.get("策略编号") == policy_number]
    if policy_version:
        records = [item for item in records if item.get("策略版本") == policy_version]
    return records


def find_activation(activation_id: str) -> dict[str, Any] | None:
    if not activation_id:
        return None
    for item in list_activations(activation_id=activation_id):
        return item
    return None


def evidence_path(activation: dict[str, Any]) -> str:
    """生效依据文件的实际路径；索引目录优先，兼容旧版资产产物目录。"""
    filename = str(activation.get("生效依据") or "")
    if not filename:
        return ""
    index_path = os.path.join(INDEX_DIR, filename)
    if os.path.exists(index_path):
        return index_path
    asset_path = os.path.join(EVIDENCE_DIR, filename)
    if os.path.exists(asset_path):
        return asset_path
    return index_path


def _linked(activation: dict[str, Any], reason: str, source: str) -> dict[str, Any]:
    return {
        "activation": activation,
        "status": SOURCE_LINKED,
        "reason": reason,
        "source": source,
    }


def _unknown(reason: str) -> dict[str, Any]:
    return {
        "activation": None,
        "status": SOURCE_UNKNOWN,
        "reason": reason,
        "source": "无",
    }


def resolve_provenance(
    task: dict[str, Any],
    activations: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """判定一条冷存任务能否对照到唯一的生效依据。

    判定顺序：
    1. 已提交/已核验的任务以冻结的 ``核验来源策略`` 为准，保证并发切换策略时
       不会改用新策略的迁移文件；
    2. 任务显式保留 ``策略生效编号`` 时直接对照生效依据；
    3. 历史回执没有策略编号时，只有该资产仅存在一条生效依据（或保留的版本/
       策略编号能唯一定位）才回填，否则标记来源不明。
    """
    if activations is None:
        records = list_activations()
    else:
        records = [dict(item) for item in activations]
    by_id = {item.get("策略生效编号"): item for item in records}

    asset_id = str(task.get("源资产") or task.get("资产编号") or "").strip()

    frozen_id = str(task.get("核验来源策略") or "").strip()
    if frozen_id and str(task.get("核验状态") or "") in (VERIFIED, "已核验"):
        frozen = by_id.get(frozen_id)
        if frozen is not None:
            return _linked(frozen, "沿用核验提交时冻结的来源策略", "核验冻结")
        # 已提交核验的来源不能被新策略顶替，冻结依据缺失时宁可判为来源不明
        return _unknown(
            f"已提交核验冻结的策略生效编号 {frozen_id} 在生效依据中缺失，不能改用新策略"
        )

    explicit = str(
        task.get("策略生效编号") or task.get("来源策略生效编号") or ""
    ).strip()
    if explicit:
        activation = by_id.get(explicit)
        if activation is None:
            return _unknown(f"策略生效编号 {explicit} 在生效依据中不存在")
        activation_asset = str(activation.get("资产编号") or "").strip()
        if asset_id and activation_asset and activation_asset != asset_id:
            return _unknown(
                f"策略生效编号 {explicit} 属于资产 {activation_asset}，与回执资产 {asset_id} 不一致"
            )
        return _linked(activation, "按回执保留的策略生效编号对照生效依据", "显式来源")

    candidates = [
        item for item in records
        if asset_id and str(item.get("资产编号") or "") == asset_id
    ]
    if len(candidates) == 1:
        return _linked(
            candidates[0],
            "历史回执未保留策略编号，按该资产唯一生效依据回填",
            "唯一生效依据",
        )
    if len(candidates) > 1:
        version = str(task.get("来源策略版本") or task.get("策略版本") or "").strip()
        policy_number = str(task.get("来源策略编号") or "").strip()
        matched = [
            item for item in candidates
            if (not version or str(item.get("策略版本") or "") == version)
            and (not policy_number or str(item.get("策略编号") or "") == policy_number)
        ]
        if (version or policy_number) and len(matched) == 1:
            return _linked(
                matched[0],
                "历史回执保留了策略版本/策略编号，可唯一定位生效依据",
                "版本回填",
            )
        return _unknown(
            "同资产存在多个生效策略，历史回执缺少策略编号，无法确定真实执行分区"
        )
    return _unknown("该资产没有可对照的策略生效依据")


def apply_provenance(task: dict[str, Any], resolution: dict[str, Any]) -> dict[str, Any]:
    """把归属判定写回任务行；不覆盖核验阶段已经冻结的来源字段。"""
    activation = resolution.get("activation")
    task["来源状态"] = resolution.get("status", SOURCE_UNKNOWN)
    task["来源说明"] = resolution.get("reason", "")
    task.setdefault("核验状态", "未核验")
    if activation is not None:
        activation_id = str(activation.get("策略生效编号") or "")
        task["策略生效编号"] = activation_id
        if str(task.get("核验状态") or "") in (VERIFIED, "已核验"):
            # 已提交核验的归属以冻结值为准，索引重建不得改写
            task["来源策略生效编号"] = str(task.get("核验来源策略") or activation_id)
            task["来源策略编号"] = str(
                task.get("来源策略编号") or activation.get("策略编号") or ""
            )
            task["来源策略版本"] = str(
                task.get("来源策略版本") or activation.get("策略版本") or ""
            )
            task["执行分区"] = str(
                task.get("执行分区") or activation.get("执行分区") or ""
            )
        else:
            task["来源策略生效编号"] = activation_id
            task["来源策略编号"] = str(activation.get("策略编号") or "")
            task["来源策略版本"] = str(activation.get("策略版本") or "")
            if not str(task.get("执行分区") or "").strip():
                task["执行分区"] = str(activation.get("执行分区") or "")
    else:
        task.setdefault("来源策略生效编号", "")
        task.setdefault("来源策略编号", "")
        task.setdefault("来源策略版本", "")
        task.setdefault("执行分区", "")
    return task


def build_index(tasks: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """根据生效依据重建分层执行数据索引，并回写任务的来源状态。

    索引行是冷存储管理筛选「本轮可提交核验对象」的唯一依据。
    """
    with _LOCK:
        activations = [dict(item) for item in _ACTIVATIONS]
        rows: list[dict[str, Any]] = []
        for index, task in enumerate(tasks, start=1):
            resolution = resolve_provenance(task, activations)
            apply_provenance(task, resolution)
            activation = resolution.get("activation")
            rows.append({
                "id": index,
                "索引编号": f"TEIX-{index:04d}",
                "资产编号": str(task.get("源资产") or ""),
                "任务编号": str(task.get("任务编号") or ""),
                "策略生效编号": (
                    str(activation.get("策略生效编号") or "")
                    if activation else str(task.get("策略生效编号") or "")
                ),
                "来源策略编号": (
                    str(activation.get("策略编号") or "")
                    if activation else str(task.get("来源策略编号") or "")
                ),
                "来源策略版本": (
                    str(activation.get("策略版本") or "")
                    if activation else str(task.get("来源策略版本") or "")
                ),
                "执行分区": str(
                    task.get("执行分区")
                    or (activation.get("执行分区") if activation else "")
                    or ""
                ),
                "核验状态": str(task.get("核验状态") or "未核验"),
                "来源状态": resolution.get("status", SOURCE_UNKNOWN),
                "来源说明": resolution.get("reason", ""),
                "生效依据": str(activation.get("生效依据") or "") if activation else "",
            })
        _write_json(INDEX_FILE, {
            "generated_at": _now(),
            "activations": len(activations),
            "entries": rows,
        })
        return rows


def load_index() -> list[dict[str, Any]]:
    """读取最近一次落盘的索引，文件不存在时返回空列表。"""
    if not os.path.exists(INDEX_FILE):
        return []
    try:
        with open(INDEX_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return []
    entries = payload.get("entries") if isinstance(payload, dict) else None
    return entries if isinstance(entries, list) else []
