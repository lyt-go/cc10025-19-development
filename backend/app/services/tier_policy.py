"""tier_policy 领域服务：分层策略管理，接收 asset_catalog 上游发布事件并生成冷存储与预热任务。

跨模块数据流：
    asset_catalog → tier_policy: 资产发布后自动生成分层策略种子
    tier_policy → cold_storage: 策略生效触发冷存储迁移
    tier_policy → hot_prefetch: 高访问频次触发预热回迁
    tier_policy → retention_rule: 分层策略关联保留规则
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


class TierPolicyService(BaseModuleService):
    MODULE = "tier_policy"
    ENTITY = "分层策略"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["策略编号", "目标资产", "当前层级", "迁移条件", "目标层级", "冷却天数", "访问频次阈值", "策略版本"]
    REQUIRED_FIELDS = ["策略编号", "目标资产", "当前层级"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用策略": "已停用"}
    NEGATIVE_ACTIONS = ["停用策略"]

    def __init__(self):
        super().__init__()
        os.makedirs(DATA_DIR, exist_ok=True)
        self._lineage_dir = os.path.join(os.path.dirname(__file__), "..", "data", "lineage")
        os.makedirs(self._lineage_dir, exist_ok=True)

    def activate_policy(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """策略生效：根据分层方向触发冷存储迁移或预热回迁。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        current_tier = str(entry.get("当前层级", ""))
        target_tier = str(entry.get("目标层级", ""))

        entry["status"] = "已生效"
        entry["pending"] = False

        asset_id = str(entry.get("目标资产", ""))
        policy_number = str(entry.get("策略编号", ""))
        policy_version = str(entry.get("策略版本", ""))

        # 每次生效分配唯一编号，并确定本次真实执行分区；同一资产多次切换策略时，
        # 冷存任务靠这个编号回指到当时的生效依据，而不是靠资产号猜。
        activation_id = execution_index.allocate_activation_id()
        execution_partition = self._execution_partition(
            asset_id, target_tier, policy_version, activation_id)
        entry["策略生效编号"] = activation_id
        entry["执行分区"] = execution_partition

        # 写入分层策略数据产物（保留按资产的最新视图，兼容既有读取方式）
        self._write_policy_record(
            asset_id, entry, execution_partition=execution_partition)

        # 生效依据按策略生效编号单独登记，旧版本不会被新版本覆盖
        activation = execution_index.record_activation(
            asset_id=asset_id,
            policy_number=policy_number,
            policy_version=policy_version,
            current_tier=current_tier,
            target_tier=target_tier,
            execution_partition=execution_partition,
            activation_id=activation_id,
        )

        # 根据分层方向触发下游动作
        if current_tier == "HOT" and target_tier == "COLD":
            # 热层 → 冷层：创建冷存储任务，并带上来源策略关系
            self._trigger_cold_storage(asset_id, entry, activation)
        elif current_tier == "COLD" and target_tier == "HOT":
            # 冷层 → 热层：创建预热回迁任务
            self._trigger_hot_prefetch(asset_id, entry, activation)
        elif target_tier == "WARM":
            # 温层：更新保留规则关联
            self._link_retention_rule(asset_id, entry)

        # 记录审计日志
        self._write_audit_entry(
            entity="分层策略",
            action="生效",
            target_id=policy_number,
            detail=(
                f"分层策略 {asset_id} {current_tier}→{target_tier} 已生效，"
                f"生效编号 {activation_id}，执行分区 {execution_partition}"
            ),
        )

        # 更新血缘图谱中的层级信息
        self._update_lineage_tier(asset_id, target_tier)

        return entry, f"{self.ENTITY} {entry_id} 已生效，{current_tier}→{target_tier}"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "标记生效":
            return self.activate_policy(entry_id)
        return super().run_action(entry_id, action)

    # ---------- 文件 I/O ----------
    def _execution_partition(self, asset_id: str, target_tier: str,
                              policy_version: str, activation_id: str) -> str:
        """按生效编号生成真实执行分区，保证每次策略切换落点可区分。"""
        tier = (target_tier or "UNKNOWN").strip().upper()
        version = (policy_version or "v1").strip()
        return f"{asset_id}/{tier}/{version}/{activation_id}"

    def _write_policy_record(self, asset_id: str, entry: dict[str, Any],
                              *, execution_partition: str = "") -> str:
        """写出生效依据：按生效编号存档一份，并按资产维护最新视图。"""
        record = {
            "activation_id": str(entry.get("策略生效编号", "")),
            "asset_id": asset_id,
            "policy_number": str(entry.get("策略编号", "")),
            "policy_version": str(entry.get("策略版本", "")),
            "current_tier": str(entry.get("当前层级", "")),
            "target_tier": str(entry.get("目标层级", "")),
            "execution_partition": execution_partition,
            "migration_condition": str(entry.get("迁移条件", "")),
            "cooling_days": str(entry.get("冷却天数", "")),
            "access_threshold": str(entry.get("访问频次阈值", "")),
            "activated_at": datetime.now(timezone.utc).isoformat(),
        }
        latest_name = f"tier_policy_{asset_id}.json"
        with open(os.path.join(DATA_DIR, latest_name), "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)

        activation_id = record["activation_id"]
        if activation_id:
            with open(os.path.join(execution_index.INDEX_DIR,
                                   execution_index.evidence_filename(activation_id)),
                      "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=2, default=str)
        return latest_name

    def read_policy_record(self, asset_id: str) -> dict[str, Any] | None:
        filename = f"tier_policy_{asset_id}.json"
        path = os.path.join(DATA_DIR, filename)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    # ---------- 跨模块副作用 ----------
    def _trigger_cold_storage(self, asset_id: str, policy: dict[str, Any],
                              activation: dict[str, Any] | None = None) -> None:
        """创建冷存储迁移任务，并写入来源策略与真实执行分区。"""
        cold_rows = store.rows("cold_storage")
        next_id = max((int(r.get("id", 0)) for r in cold_rows), default=0) + 1
        activation = activation or {}
        task = {
            "id": next_id,
            "status": "待调度",
            "pending": True,
            "abnormal": False,
            "任务编号": f"COLD-{next_id:04d}",
            "源资产": asset_id,
            "目标存储": "S3-GLACIER",
            "压缩算法": "ZSTD",
            "迁移量": "估算中",
            "预计耗时": "估算中",
            "迁移状态": "待调度",
            "校验摘要": "",
            # 分层执行数据索引字段：来源策略生效编号 + 真实执行分区
            "策略生效编号": str(activation.get("策略生效编号") or ""),
            "来源策略编号": str(activation.get("策略编号") or ""),
            "来源策略版本": str(activation.get("策略版本") or ""),
            "执行分区": str(
                activation.get("执行分区")
                or policy.get("执行分区")
                or ""
            ),
            "来源状态": execution_index.SOURCE_LINKED,
            "核验状态": "未核验",
        }
        cold_rows.append(task)
        # 新增任务立即进入分层执行数据索引，冷存储管理可直接按生效编号筛选
        execution_index.build_index(cold_rows)

    def _trigger_hot_prefetch(self, asset_id: str, policy: dict[str, Any],
                              activation: dict[str, Any] | None = None) -> None:
        """创建预热回迁任务。"""
        hot_rows = store.rows("hot_prefetch")
        next_id = max((int(r.get("id", 0)) for r in hot_rows), default=0) + 1
        task = {
            "id": next_id,
            "status": "排队中",
            "pending": True,
            "abnormal": False,
            "任务编号": f"HOT_-{next_id:04d}",
            "冷存位置": f"S3://glacier/{asset_id}",
            "目标分区": f"{asset_id}_hot",
            "回迁量": "估算中",
            "预计耗时": "估算中",
            "优先级": "高",
            "过期策略": "T+30",
            "任务状态": "排队中",
        }
        hot_rows.append(task)

    def _link_retention_rule(self, asset_id: str, policy: dict[str, Any]) -> None:
        """为温层资产关联保留规则。"""
        retention_rows = store.rows("retention_rule")
        next_id = max((int(r.get("id", 0)) for r in retention_rows), default=0) + 1
        cooling = str(policy.get("冷却天数", "90"))
        rule = {
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "规则编号": f"RETE-{next_id:04d}",
            "目标资产": asset_id,
            "保留天数": cooling,
            "归档路径": f"archive://{asset_id}",
            "压缩策略": "ZSTD",
            "豁免条件": "",
            "执行频率": "monthly",
            "规则版本": "v1",
        }
        retention_rows.append(rule)

    def _update_lineage_tier(self, asset_id: str, target_tier: str) -> None:
        """更新血缘图谱中的分层信息。"""
        lineage_rows = store.rows("lineage_graph")
        for row in lineage_rows:
            if str(row.get("下游资产")) == asset_id:
                row["血缘状态"] = f"Tier:{target_tier}"
                break

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