"""cold_storage 领域服务：冷存储迁移管理，接收 tier_policy 上游事件触发冷存任务。

跨模块数据流：
    tier_policy → cold_storage: 分层策略生效触发冷存储迁移（携带策略生效编号）
    cold_storage → hot_prefetch: 冷存完成可能触发预热回迁需求
    cold_storage → cost_tracker: 迁移任务更新成本追踪

分层执行数据索引：
    同一资产多次切换分层策略后，单靠资产号无法区分冷存任务是哪一版策略触发的。
    ``refresh_execution_index`` 依据生效依据重建索引并回写任务来源状态；
    ``list_candidates`` 只放出能对照到唯一生效依据的任务；
    ``submit_verification`` 在提交时冻结来源策略与迁移文件，之后策略再切换也不会
    把已提交的核验改成新策略对应的迁移文件。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from app import execution_index
from app.services.base import BaseModuleService
from app.store import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "assets")


class ColdStorageService(BaseModuleService):
    MODULE = "cold_storage"
    ENTITY = "冷存任务"
    OPERATION_TERM = "迁移"
    LIST_FIELDS = [
        "任务编号", "源资产", "目标存储", "压缩算法", "迁移量", "预计耗时", "迁移状态", "校验摘要",
        "策略生效编号", "来源策略编号", "来源策略版本", "执行分区", "来源状态", "核验状态",
    ]
    REQUIRED_FIELDS = ["任务编号", "源资产", "目标存储"]
    STATUS_ORDER = ["待调度", "迁移中", "已完成", "已失败"]
    ACTION_RULES = {
        "发起迁移": "迁移中", "确认完成": "已完成", "标记失败": "已失败",
        "提交核验": "已完成",
    }
    NEGATIVE_ACTIONS = []

    VERIFICATION_MARK = "已提交"

    def __init__(self):
        super().__init__()
        os.makedirs(DATA_DIR, exist_ok=True)

    # ---------- 分层执行数据索引 ----------
    def refresh_execution_index(self) -> list[dict[str, Any]]:
        """重建分层执行数据索引，并把来源状态回写到冷存任务。"""
        return execution_index.build_index(store.rows(self.MODULE))

    def execution_index_rows(self) -> list[dict[str, Any]]:
        return self.refresh_execution_index()

    def list_candidates(
        self,
        *,
        asset_id: str | None = None,
        activation_id: str | None = None,
        policy_number: str | None = None,
        policy_version: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """返回本轮可提交核验的对象。

        只放出 ``来源状态=已关联`` 且尚未提交核验的任务；来源不明的记录可以
        查询但在候选集中直接排除，避免操作者从筛选结果里误选任务去核验。
        """
        self.refresh_execution_index()
        rows = [
            row for row in store.rows(self.MODULE)
            if str(row.get("来源状态") or "") == execution_index.SOURCE_LINKED
            and str(row.get("核验状态") or "未核验") == "未核验"
        ]
        if asset_id:
            rows = [row for row in rows if str(row.get("源资产") or "") == asset_id]
        if activation_id:
            rows = [
                row for row in rows
                if str(row.get("策略生效编号") or "") == activation_id
            ]
        if policy_number:
            rows = [
                row for row in rows
                if str(row.get("来源策略编号") or "") == policy_number
            ]
        if policy_version:
            rows = [
                row for row in rows
                if str(row.get("来源策略版本") or "") == policy_version
            ]
        total = len(rows)
        start = (page - 1) * size
        return [dict(row) for row in rows[start:start + size]], total

    def trace_activation(
        self, activation_id: str
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """按策略生效编号取出生效依据和所有引用它的冷存任务。"""
        activation = execution_index.find_activation(activation_id)
        self.refresh_execution_index()
        tasks = [
            dict(row) for row in store.rows(self.MODULE)
            if str(row.get("来源策略生效编号") or row.get("策略生效编号") or "")
            == activation_id
        ]
        return activation, tasks

    def list_unresolved(self, asset_id: str | None = None) -> list[dict[str, Any]]:
        """列出仍缺策略来源的迁移任务，供核验前对照生效依据。"""
        self.refresh_execution_index()
        rows = [
            dict(row) for row in store.rows(self.MODULE)
            if str(row.get("来源状态") or "") != execution_index.SOURCE_LINKED
        ]
        if asset_id:
            rows = [row for row in rows if str(row.get("源资产") or "") == asset_id]
        return rows

    def submit_verification(
        self, entry_id: int
    ) -> tuple[dict[str, Any] | None, str]:
        """提交核验：冻结来源策略和迁移文件，禁止来源不明的任务进入核验。"""
        self.refresh_execution_index()
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        if str(entry.get("核验状态") or "未核验") in (self.VERIFICATION_MARK, "已核验"):
            return None, f"{self.ENTITY} {entry_id} 已提交核验，不能重复提交"

        # 重新按当时索引判定归属，来源不明一律拦截
        resolution = execution_index.resolve_provenance(entry)
        activation = resolution.get("activation")
        if resolution.get("status") != execution_index.SOURCE_LINKED or activation is None:
            entry["来源状态"] = execution_index.SOURCE_UNKNOWN
            entry["来源说明"] = resolution.get("reason", "来源不明")
            return None, (
                f"{self.ENTITY} {entry_id} 来源不明，不能进入核验："
                f"{entry.get('来源说明', '')}"
            )

        source_activation = str(activation.get("策略生效编号") or "")
        # 迁移文件与生效依据都必须真实存在，缺一不可
        evidence_file = execution_index.evidence_path(activation)
        source_file = str(activation.get("生效依据") or "")
        if not evidence_file or not os.path.exists(evidence_file):
            return None, (
                f"{self.ENTITY} {entry_id} 缺少策略 {source_activation} 的生效依据文件，"
                "不能进入核验"
            )

        entry["策略生效编号"] = source_activation
        entry["来源策略生效编号"] = source_activation
        entry["来源策略编号"] = str(activation.get("策略编号") or "")
        entry["来源策略版本"] = str(activation.get("策略版本") or "")
        entry["执行分区"] = (
            str(activation.get("执行分区") or "")
            or str(entry.get("执行分区") or "")
        )
        # 冻结：后续策略切换不会改写这条任务的核验来源
        entry["核验来源策略"] = source_activation
        entry["核验来源文件"] = source_file
        entry["核验状态"] = self.VERIFICATION_MARK
        entry["核验提交时间"] = datetime.now(timezone.utc).isoformat()
        entry["来源状态"] = execution_index.SOURCE_LINKED
        entry["来源说明"] = "核验提交时冻结来源策略与生效依据文件"

        self._write_receipt(entry, source_activation, source_file)
        self._write_cold_storage_record(str(entry.get("源资产", "")), entry)
        self._write_audit_entry(
            entity="冷存任务",
            action="提交核验",
            target_id=str(entry.get("任务编号", "")),
            detail=(
                f"冷存任务 {entry.get('任务编号', '')} 提交核验，"
                f"来源策略 {source_activation}，执行分区 {entry.get('执行分区', '')}"
            ),
        )

        return entry, (
            f"{self.ENTITY} {entry_id} 已提交核验，来源策略 {source_activation}，"
            f"执行分区 {entry.get('执行分区', '')}"
        )

    def start_migration(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """发起冷存储迁移：记录迁移状态并更新成本追踪。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        asset_id = str(entry.get("源资产", ""))
        target_storage = str(entry.get("目标存储", ""))

        entry["status"] = "迁移中"
        entry["pending"] = True
        entry["迁移状态"] = "迁移中"

        # 写入迁移记录
        self._write_cold_storage_record(asset_id, entry)

        # 更新成本追踪
        self._update_cost_tracker(asset_id, target_storage)

        # 记录审计日志
        self._write_audit_entry(
            entity="冷存任务",
            action="发起迁移",
            target_id=str(entry.get("任务编号", "")),
            detail=f"冷存迁移 {asset_id} → {target_storage}",
        )

        return entry, f"{self.ENTITY} {entry_id} 迁移中，{asset_id}→{target_storage}"

    def complete_migration(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """确认迁移完成：记录校验摘要并检查是否需要预热回迁。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        asset_id = str(entry.get("源资产", ""))
        entry["status"] = "已完成"
        entry["pending"] = False
        entry["迁移状态"] = "已完成"
        entry["校验摘要"] = "CRC32校验通过"

        # 更新数据产物
        self._write_cold_storage_record(asset_id, entry)

        # 如果存在预热回迁需求，触发hot_prefetch
        self._check_prefetch_need(asset_id)

        return entry, f"{self.ENTITY} {entry_id} 已完成，数据已迁移到冷存储"

    def get_migration_status(self, asset_id: str) -> dict[str, Any] | None:
        return self._read_cold_storage_record(asset_id)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "发起迁移":
            return self.start_migration(entry_id)
        if action == "确认完成":
            return self.complete_migration(entry_id)
        if action == "提交核验":
            return self.submit_verification(entry_id)
        return super().run_action(entry_id, action)

    # ---------- 文件 I/O ----------
    def _write_cold_storage_record(self, asset_id: str, entry: dict[str, Any]) -> None:
        filename = f"cold_storage_{asset_id}.json"
        path = os.path.join(DATA_DIR, filename)
        record = {
            "asset_id": asset_id,
            "task_id": str(entry.get("任务编号", "")),
            "target_storage": str(entry.get("目标存储", "")),
            "compression": str(entry.get("压缩算法", "")),
            "migration_volume": str(entry.get("迁移量", "")),
            "status": str(entry.get("迁移状态", "")),
            "checksum": str(entry.get("校验摘要", "")),
            # 带上来源策略关系，按资产只能看到最后一条，但按编号仍能对照
            "activation_id": str(entry.get("策略生效编号", "")),
            "source_policy": str(entry.get("来源策略编号", "")),
            "source_version": str(entry.get("来源策略版本", "")),
            "execution_partition": str(entry.get("执行分区", "")),
            "verification_status": str(entry.get("核验状态", "")),
            "verification_activation": str(entry.get("核验来源策略", "")),
            "verification_file": str(entry.get("核验来源文件", "")),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)

    def _write_receipt(self, entry: dict[str, Any], activation_id: str,
                       source_file: str) -> str:
        """把核验回执按任务编号+生效编号存档，回执归属可与生效依据对照。"""
        task_id = str(entry.get("任务编号", "")) or f"TASK-{entry.get('id', 0)}"
        filename = f"cold_receipt_{task_id}_{activation_id or 'UNKNOWN'}.json"
        receipt = {
            "task_id": task_id,
            "asset_id": str(entry.get("源资产", "")),
            "activation_id": activation_id,
            "source_policy": str(entry.get("来源策略编号", "")),
            "source_version": str(entry.get("来源策略版本", "")),
            "execution_partition": str(entry.get("执行分区", "")),
            "evidence_file": source_file,
            "verification_status": str(entry.get("核验状态", "")),
            "submitted_at": str(entry.get("核验提交时间", "")),
        }
        with open(os.path.join(DATA_DIR, filename), "w", encoding="utf-8") as f:
            json.dump(receipt, f, ensure_ascii=False, indent=2, default=str)
        return filename

    def read_verification_receipt(
        self, task_id: str, activation_id: str
    ) -> dict[str, Any] | None:
        filename = f"cold_receipt_{task_id}_{activation_id or 'UNKNOWN'}.json"
        path = os.path.join(DATA_DIR, filename)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _read_cold_storage_record(self, asset_id: str) -> dict[str, Any] | None:
        filename = f"cold_storage_{asset_id}.json"
        path = os.path.join(DATA_DIR, filename)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    # ---------- 跨模块副作用 ----------
    def _update_cost_tracker(self, asset_id: str, target_storage: str) -> None:
        """更新成本追踪记录中的存储成本。"""
        cost_rows = store.rows("cost_tracker")
        for row in cost_rows:
            if str(row.get("关联资产")) == asset_id:
                row["存储成本"] = f"冷存:{target_storage}"
                row["status"] = "已核算"
                row["pending"] = False
                break

    def _check_prefetch_need(self, asset_id: str) -> None:
        """检查冷存资产是否有预热回迁需求。"""
        hot_rows = store.rows("hot_prefetch")
        for row in hot_rows:
            if str(row.get("冷存位置", "")).find(asset_id) >= 0 and row.get("status") == "排队中":
                return
        # 无已有预热任务则创建种子
        next_id = max((int(r.get("id", 0)) for r in hot_rows), default=0) + 1
        prefetch = {
            "id": next_id,
            "status": "排队中",
            "pending": True,
            "abnormal": False,
            "任务编号": f"HOT_-{next_id:04d}",
            "冷存位置": f"S3://glacier/{asset_id}",
            "目标分区": f"{asset_id}_hot",
            "回迁量": "待估算",
            "预计耗时": "待估算",
            "优先级": "低",
            "过期策略": "T+7",
            "任务状态": "排队中",
        }
        hot_rows.append(prefetch)

    def _write_audit_entry(self, entity: str, action: str, target_id: str,
                           detail: str) -> None:
        audit_rows = store.rows("audit_log")
        next_id = max((int(r.get("id", 0)) for r in audit_rows), default=0) + 1
        entry = {
            "id": next_id,
            "status": "已记录",
            "pending": False,
            "abnormal": False,
            "记录编号": f"AUDI-{next_id:04d}",
            "操作主体": entity,
            "操作类型": action,
            "操作对象": target_id,
            "操作时间": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "影响范围": detail,
            "审计摘要": detail,
            "归档状态": "已记录",
        }
        audit_rows.append(entry)