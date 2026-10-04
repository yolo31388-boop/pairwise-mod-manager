"""
模组管理与加载系统 - 核心模块

功能：
- 模组加载：完整加载脚本/资源/配置/本地化，按依赖拓扑排序，沙箱隔离文件访问
- 冲突检测：检测多模组修改同一文件，区分覆盖/修改类型并给出解决方案
- 模组卸载：需确认，卸载后清理干净模组文件
- 版本检查：语义化版本兼容性检查（容忍小版本更新），支持自动更新
- 模组配置：读写配置、持久化保存、生成配置界面描述
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import json
import os
import re


class SandboxViolation(PermissionError):
    """模组试图访问其沙箱目录之外的文件。"""


class DependencyError(RuntimeError):
    """模组依赖缺失或存在循环依赖。"""


@dataclass
class Config:
    pass


_VERSION_RE = re.compile(r"\d+")
_UNSET = object()


def parse_version(version) -> Tuple[int, int, int]:
    """把版本字符串解析为 (major, minor, patch) 三元组。"""
    parts = _VERSION_RE.findall(str(version))[:3]
    nums = [int(p) for p in parts]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


@dataclass
class Mod:
    id: str
    name: str = ""
    version: str = "1.0.0"
    dependencies: List[str] = field(default_factory=list)
    scripts: List[str] = field(default_factory=list)
    resources: Dict[str, str] = field(default_factory=dict)
    localization: Dict[str, Dict[str, str]] = field(default_factory=dict)
    config: Dict = field(default_factory=dict)
    config_schema: Dict = field(default_factory=dict)
    # 文件相对路径 -> 改动类型（"overwrite" 覆盖 / "modify" 修改）
    modifies: Dict[str, str] = field(default_factory=dict)
    # 安装到游戏目录的文件（相对路径）
    files: List[str] = field(default_factory=list)
    min_game_version: str = "0.0.0"
    max_game_version: Optional[str] = None
    # 自动更新信息：可更新到的最新版本及其兼容范围
    latest_version: Optional[str] = None
    latest_min_game_version: Optional[str] = None
    latest_max_game_version: Optional[str] = None
    priority: int = 0


class ModManager:
    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self._state = {}
        self._history = []
        self.game_dir = self.config.get("game_dir", "")
        self.mods_dir = self.config.get(
            "mods_dir", os.path.join(self.game_dir, "mods") if self.game_dir else ""
        )
        self._config_path = self.config.get("config_path") or (
            os.path.join(self.game_dir, "mod_configs.json") if self.game_dir else None
        )
        self.auto_update = self.config.get("auto_update", True)
        self._installed: Dict[str, Mod] = {}
        self._loaded: Dict[str, Dict] = {}
        self._mod_configs: Dict[str, Dict] = {}
        self._load_configs()

    # ------------------------------------------------------------------
    # 基础生命周期
    # ------------------------------------------------------------------
    def update(self, dt: float):
        pass

    def reset(self):
        self._state = {}
        self._history = []
        self._installed = {}
        self._loaded = {}
        self._mod_configs = {}
        self._load_configs()

    @staticmethod
    def _normalize(mod) -> Mod:
        if isinstance(mod, Mod):
            return mod
        if isinstance(mod, dict):
            return Mod(**mod)
        raise TypeError("mod 必须是 Mod 实例或 dict")

    # ------------------------------------------------------------------
    # 模组加载：完整内容 + 依赖顺序 + 沙箱
    # ------------------------------------------------------------------
    def load_mod(self, mod) -> Dict:
        """完整加载单个模组：脚本、资源、配置、本地化全部加载。

        依赖未先加载时抛出 DependencyError，保证加载顺序。
        """
        mod = self._normalize(mod)
        missing = [d for d in mod.dependencies if d not in self._loaded]
        if missing:
            raise DependencyError(
                "模组 {} 的依赖尚未加载: {}".format(mod.id, ", ".join(missing))
            )
        merged_config = dict(mod.config)
        merged_config.update(self._mod_configs.get(mod.id, {}))
        result = {
            "id": mod.id,
            "scripts": list(mod.scripts),
            "resources": dict(mod.resources),
            "config": merged_config,
            "localization": {
                lang: dict(strings) for lang, strings in mod.localization.items()
            },
            "sandbox": self._sandbox_root(mod),
        }
        self._loaded[mod.id] = result
        self._installed.setdefault(mod.id, mod)
        self._mod_configs[mod.id] = merged_config
        return result

    def resolve_load_order(self, mods) -> List[str]:
        """按依赖关系拓扑排序，返回模组 id 的加载顺序。"""
        mods = [self._normalize(m) for m in mods]
        by_id = {m.id: m for m in mods}
        for m in mods:
            for dep in m.dependencies:
                if dep not in by_id:
                    raise DependencyError(
                        "模组 {} 依赖未提供的模组 {}".format(m.id, dep)
                    )
        indegree = {m.id: 0 for m in mods}
        dependents = {m.id: [] for m in mods}
        for m in mods:
            for dep in m.dependencies:
                indegree[m.id] += 1
                dependents[dep].append(m.id)
        ready = sorted(
            [mid for mid, deg in indegree.items() if deg == 0],
            key=lambda mid: (by_id[mid].priority, mid),
        )
        order = []
        while ready:
            mid = ready.pop(0)
            order.append(mid)
            for child in dependents[mid]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
            ready.sort(key=lambda mid2: (by_id[mid2].priority, mid2))
        if len(order) != len(mods):
            raise DependencyError("模组依赖存在循环，无法确定加载顺序")
        return order

    def load_mods(self, mods) -> List[str]:
        """按依赖顺序完整加载一批模组，返回实际加载顺序。"""
        mods = [self._normalize(m) for m in mods]
        by_id = {m.id: m for m in mods}
        order = self.resolve_load_order(mods)
        for mid in order:
            self.load_mod(by_id[mid])
        return order

    def _sandbox_root(self, mod: Mod) -> str:
        root = (
            os.path.join(self.mods_dir, mod.id)
            if self.mods_dir
            else os.path.abspath(mod.id)
        )
        return os.path.abspath(root)

    def mod_access_file(self, mod_id: str, path: str) -> str:
        """沙箱文件访问：模组只能访问自己沙箱目录内的文件。

        返回解析后的绝对路径；越界访问抛出 SandboxViolation。
        """
        if mod_id not in self._installed:
            raise KeyError("模组未安装: {}".format(mod_id))
        root = self._sandbox_root(self._installed[mod_id])
        candidate = path if os.path.isabs(path) else os.path.join(root, path)
        resolved = os.path.abspath(candidate)
        if resolved != root and not resolved.startswith(root + os.sep):
            raise SandboxViolation(
                "模组 {} 试图访问沙箱之外的文件: {}".format(mod_id, path)
            )
        return resolved

    # ------------------------------------------------------------------
    # 冲突检测：按文件分组，区分冲突类型，给出解决方案
    # ------------------------------------------------------------------
    def conflict_detection(self, mods) -> List[Dict]:
        """检测多个模组之间的文件冲突。

        返回冲突列表，每项包含冲突文件、涉及模组、冲突类型
        （overwrite=硬冲突 / modify=可合并的软冲突）以及可选解决方案。
        """
        mods = [self._normalize(m) for m in mods]
        file_map: Dict[str, List[Tuple[str, str]]] = {}
        for m in mods:
            for path, change_type in m.modifies.items():
                file_map.setdefault(path, []).append((m.id, change_type))
        conflicts = []
        for path, entries in sorted(file_map.items()):
            if len(entries) < 2:
                continue
            mod_ids = [mid for mid, _ in entries]
            types = {t for _, t in entries}
            if "overwrite" in types:
                conflict_type = "overwrite"
                resolutions = [
                    {
                        "action": "set_priority",
                        "description": "调整加载顺序，让高优先级模组最后覆盖",
                        "mods": mod_ids,
                    },
                    {
                        "action": "disable_mod",
                        "description": "禁用其中一个模组，只保留一个覆盖该文件",
                        "mods": mod_ids,
                    },
                ]
            else:
                conflict_type = "modify"
                resolutions = [
                    {
                        "action": "merge_patches",
                        "description": "合并两个模组对该文件的修改补丁",
                        "mods": mod_ids,
                    },
                    {
                        "action": "set_priority",
                        "description": "调整加载顺序，后加载的修改先生效",
                        "mods": mod_ids,
                    },
                ]
            conflicts.append(
                {
                    "file": path,
                    "mods": mod_ids,
                    "type": conflict_type,
                    "resolutions": resolutions,
                }
            )
        return conflicts

    # ------------------------------------------------------------------
    # 模组安装 / 卸载
    # ------------------------------------------------------------------
    def install_mod(self, mod) -> Dict:
        """安装模组：登记并把模组文件写入游戏目录。"""
        mod = self._normalize(mod)
        self._installed[mod.id] = mod
        written = []
        if self.game_dir:
            for rel in mod.files:
                path = os.path.join(self.game_dir, rel)
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(mod.resources.get(rel, ""))
                written.append(rel)
        return {"mod_id": mod.id, "installed_files": written}

    def mod_uninstall(self, mod_id: str, confirm: bool = False) -> Dict:
        """卸载模组。需要 confirm=True 确认；卸载时清理干净模组文件。"""
        if mod_id not in self._installed:
            raise KeyError("模组未安装: {}".format(mod_id))
        if not confirm:
            return {
                "status": "confirmation_required",
                "mod_id": mod_id,
                "message": "确认卸载模组 {}？请使用 confirm=True 再次调用。".format(mod_id),
            }
        mod = self._installed[mod_id]
        removed_files = []
        for rel in mod.files:
            path = os.path.join(self.game_dir, rel) if self.game_dir else rel
            if os.path.exists(path):
                os.remove(path)
                removed_files.append(rel)
        del self._installed[mod_id]
        self._loaded.pop(mod_id, None)
        self._mod_configs.pop(mod_id, None)
        self._save_configs()
        return {
            "status": "uninstalled",
            "mod_id": mod_id,
            "removed_files": removed_files,
        }

    # ------------------------------------------------------------------
    # 版本检查：兼容性判断 + 自动更新
    # ------------------------------------------------------------------
    def version_check(self, mod, game_version: str) -> Dict:
        """检查模组与游戏版本是否兼容。

        同一主版本内的小版本/补丁更新视为兼容；主版本不兼容时，
        若模组提供了可用的新版本则自动更新。
        """
        mod = self._normalize(mod)
        game = parse_version(game_version)
        result = {
            "mod_id": mod.id,
            "mod_version": mod.version,
            "game_version": game_version,
            "compatible": True,
            "updated": False,
            "reason": "",
        }
        if game < parse_version(mod.min_game_version):
            result["compatible"] = False
            result["reason"] = "游戏版本低于模组要求的最低版本"
        elif mod.max_game_version is not None:
            maxv = parse_version(mod.max_game_version)
            # 同一主版本内的小版本更新仍然兼容，只有主版本越界才不兼容
            if game > maxv and game[0] != maxv[0]:
                result["compatible"] = False
                result["reason"] = "游戏主版本超出模组支持范围"
        if not result["compatible"] and self.auto_update and mod.latest_version:
            new_min = parse_version(mod.latest_min_game_version or "0.0.0")
            update_ok = game >= new_min
            if update_ok and mod.latest_max_game_version is not None:
                new_max = parse_version(mod.latest_max_game_version)
                update_ok = game <= new_max or game[0] == new_max[0]
            if update_ok:
                mod.version = mod.latest_version
                result.update(
                    compatible=True,
                    updated=True,
                    mod_version=mod.latest_version,
                    reason="已自动更新到兼容版本",
                )
        return result

    # ------------------------------------------------------------------
    # 模组配置：读写 + 持久化 + 界面描述
    # ------------------------------------------------------------------
    def mod_config(self, mod_id: str, key: Optional[str] = None, value=_UNSET):
        """读取或设置模组配置。设置后自动持久化保存。"""
        if mod_id not in self._installed:
            raise KeyError("模组未安装: {}".format(mod_id))
        cfg = self._mod_configs.setdefault(
            mod_id, dict(self._installed[mod_id].config)
        )
        if key is None:
            return dict(cfg)
        if value is _UNSET:
            return cfg.get(key)
        cfg[key] = value
        self._save_configs()
        return value

    def get_config_ui(self, mod_id: str) -> Dict:
        """生成模组配置界面描述（控件列表），供前端渲染设置界面。"""
        if mod_id not in self._installed:
            raise KeyError("模组未安装: {}".format(mod_id))
        mod = self._installed[mod_id]
        cfg = self.mod_config(mod_id)
        schema = mod.config_schema or {}
        keys = list(schema.keys()) or list(cfg.keys())
        widgets = []
        for key in keys:
            meta = dict(schema.get(key, {}))
            current = cfg.get(key, meta.get("default"))
            widget = {
                "key": key,
                "label": meta.get("label", key),
                "widget": meta.get("widget") or self._infer_widget(meta, current),
                "value": current,
            }
            for extra in ("min", "max", "step", "options"):
                if extra in meta:
                    widget[extra] = meta[extra]
            widgets.append(widget)
        return {
            "mod_id": mod_id,
            "title": mod.name or mod.id,
            "widgets": widgets,
        }

    @staticmethod
    def _infer_widget(meta: Dict, value) -> str:
        if meta.get("options"):
            return "dropdown"
        if isinstance(value, bool):
            return "checkbox"
        if isinstance(value, (int, float)):
            return "slider" if "min" in meta and "max" in meta else "number"
        return "text"

    # ------------------------------------------------------------------
    # 配置持久化
    # ------------------------------------------------------------------
    def _load_configs(self):
        if self._config_path and os.path.exists(self._config_path):
            with open(self._config_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._mod_configs.update(data)

    def _save_configs(self):
        if not self._config_path:
            return
        directory = os.path.dirname(self._config_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self._config_path, "w", encoding="utf-8") as fh:
            json.dump(self._mod_configs, fh, ensure_ascii=False, indent=2)
