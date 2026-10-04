"""
模组管理与加载系统 - 核心模块

功能：
- 模组完整加载（脚本/资源/配置/本地化），依赖拓扑排序，沙箱隔离
- 冲突检测（区分覆盖/修改类型，给出解决方案）
- 模组卸载（需确认，干净还原游戏文件）
- 版本检查（语义化版本兼容判断，支持自动更新）
- 模组配置（可调整、持久化保存、带文本设置界面）
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
import json
import os
import time


class ModError(Exception):
    """模组系统基础异常"""


class ModDependencyError(ModError):
    """依赖缺失或循环依赖"""


class SandboxViolation(ModError):
    """模组试图访问沙箱外的文件"""


class UninstallConfirmationRequired(ModError):
    """卸载操作未经过确认"""


@dataclass
class Config:
    game_version: str = "1.0.0"
    config_path: Optional[str] = None
    auto_update: bool = True
    sandbox_allowed: Tuple[str, ...] = ("shared/",)


@dataclass
class Mod:
    mod_id: str
    name: str = ""
    version: str = "1.0.0"
    min_game_version: str = "1.0.0"
    max_game_version: Optional[str] = None
    dependencies: List[str] = field(default_factory=list)
    scripts: Dict[str, str] = field(default_factory=dict)
    resources: Dict[str, str] = field(default_factory=dict)
    config_files: Dict[str, str] = field(default_factory=dict)
    localizations: Dict[str, str] = field(default_factory=dict)
    # 模组默认设置项（键值对，玩家可通过 mod_config 调整）
    configs: Dict[str, Any] = field(default_factory=dict)
    # 声明式游戏文件改动：路径 -> "override"（覆盖）或 "modify"（修改）
    file_changes: Dict[str, str] = field(default_factory=dict)
    # 可用更新信息：{"version": ..., "min_game_version": ..., "max_game_version": ...}
    update_info: Optional[Dict[str, str]] = None


class ModManager:
    CHANGE_OVERRIDE = "override"
    CHANGE_MODIFY = "modify"

    CONFLICT_OVERRIDE = "override"
    CONFLICT_MODIFY = "modify"
    CONFLICT_MIXED = "mixed"

    _RESOLUTIONS = {
        CONFLICT_OVERRIDE: "多个模组覆盖同一文件：请设置加载优先级，后加载的高优先级模组生效",
        CONFLICT_MODIFY: "多个模组修改同一文件：修改可以合并，建议按依赖关系调整加载顺序",
        CONFLICT_MIXED: "覆盖与修改冲突：覆盖会丢弃其他模组的修改，建议让修改型模组以补丁形式在覆盖之后加载",
    }

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self._state: Dict[str, Dict] = {}
        self._history: List[Tuple] = []

        self.game_version: str = self.config.get("game_version", "1.0.0")
        self.auto_update: bool = self.config.get("auto_update", True)
        self._config_path: Optional[str] = self.config.get("config_path")
        self._sandbox_allowed: Tuple[str, ...] = tuple(
            self.config.get("sandbox_allowed", ("shared/",))
        )

        self._mods: Dict[str, Mod] = {}
        self._load_order: List[str] = []
        self._game_files: Dict[str, str] = {}
        self._installed_files: Dict[str, List[str]] = {}
        self._file_backups: Dict[Tuple[str, str], Optional[str]] = {}
        self._mod_configs: Dict[str, Dict[str, Any]] = {}
        self._load_saved_configs()

    # ------------------------------------------------------------------
    # 基础生命周期
    # ------------------------------------------------------------------
    def update(self, dt: float):
        pass

    def reset(self):
        self._state = {}
        self._history = []

    # ------------------------------------------------------------------
    # 版本工具
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_version(version: str) -> Tuple[int, int, int]:
        parts = []
        for piece in str(version).split("."):
            digits = ""
            for ch in piece:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            parts.append(int(digits) if digits else 0)
        while len(parts) < 3:
            parts.append(0)
        return tuple(parts[:3])

    # ------------------------------------------------------------------
    # 沙箱
    # ------------------------------------------------------------------
    def _sandbox_check(self, mod_id: str, path: str) -> str:
        """校验模组资源路径，返回规范化路径；越界访问抛出 SandboxViolation。"""
        if not path or os.path.isabs(path) or (len(path) > 1 and path[1] == ":"):
            raise SandboxViolation(f"模组 {mod_id} 使用了非法绝对路径: {path!r}")
        normalized = os.path.normpath(path).replace(os.sep, "/")
        if normalized == ".." or normalized.startswith("../"):
            raise SandboxViolation(f"模组 {mod_id} 试图越出沙箱访问: {path!r}")
        allowed_prefixes = (f"mods/{mod_id}/",) + tuple(self._sandbox_allowed)
        if not any(normalized.startswith(p) for p in allowed_prefixes):
            raise SandboxViolation(
                f"模组 {mod_id} 无权访问沙箱外路径: {path!r}（允许: {allowed_prefixes}）"
            )
        return normalized

    def sandbox_access(self, mod_id: str, path: str) -> str:
        """运行期沙箱访问入口，供模组读取自身资源。"""
        return self._sandbox_check(mod_id, path)

    # ------------------------------------------------------------------
    # 模组加载
    # ------------------------------------------------------------------
    def load_mod(self, mod: Mod) -> Dict[str, Dict[str, str]]:
        """完整加载单个模组：脚本、资源、配置、本地化，并应用声明的文件改动。"""
        missing = [d for d in mod.dependencies if d not in self._mods]
        if missing:
            raise ModDependencyError(
                f"模组 {mod.mod_id} 的依赖未加载: {', '.join(missing)}"
            )

        entry: Dict[str, Dict[str, str]] = {
            "scripts": {},
            "resources": {},
            "configs": {},
            "localizations": {},
        }
        installed = self._installed_files.setdefault(mod.mod_id, [])
        for kind, files in (
            ("scripts", mod.scripts),
            ("resources", mod.resources),
            ("configs", mod.config_files),
            ("localizations", mod.localizations),
        ):
            for path, content in files.items():
                safe = self._sandbox_check(mod.mod_id, path)
                entry[kind][safe] = content
                installed.append(safe)

        for path, change in mod.file_changes.items():
            normalized = os.path.normpath(path).replace(os.sep, "/")
            key = (mod.mod_id, normalized)
            if key not in self._file_backups:
                self._file_backups[key] = self._game_files.get(normalized)
            if change == self.CHANGE_OVERRIDE:
                self._game_files[normalized] = f"override:{mod.mod_id}"
            elif change == self.CHANGE_MODIFY:
                base = self._game_files.get(normalized, "")
                self._game_files[normalized] = base + f"+modify:{mod.mod_id}"
            else:
                raise ModError(f"模组 {mod.mod_id} 未知的改动类型: {change!r}")
            installed.append(normalized)

        self._mods[mod.mod_id] = mod
        self._load_order.append(mod.mod_id)
        self._state[mod.mod_id] = entry
        self._mod_configs.setdefault(mod.mod_id, dict(mod.configs))
        self._history.append(("load", mod.mod_id, time.time()))
        return entry

    def load_mods(self, mods: List[Mod]) -> List[str]:
        """按依赖拓扑顺序批量加载模组，返回实际加载顺序。"""
        by_id = {m.mod_id: m for m in mods}
        order: List[str] = []
        mark: Dict[str, str] = {}

        def visit(mod_id: str, stack: List[str]):
            state = mark.get(mod_id)
            if state == "done":
                return
            if state == "visiting":
                cycle = " -> ".join(stack + [mod_id])
                raise ModDependencyError(f"检测到循环依赖: {cycle}")
            if mod_id not in by_id:
                if mod_id in self._mods:
                    mark[mod_id] = "done"
                    return
                raise ModDependencyError(f"缺少依赖模组: {mod_id}")
            mark[mod_id] = "visiting"
            for dep in by_id[mod_id].dependencies:
                visit(dep, stack + [mod_id])
            mark[mod_id] = "done"
            order.append(mod_id)

        for m in mods:
            visit(m.mod_id, [])

        for mod_id in order:
            if mod_id not in self._mods:
                self.load_mod(by_id[mod_id])
        return order

    @property
    def load_order(self) -> List[str]:
        return list(self._load_order)

    def get_game_file(self, path: str) -> Optional[str]:
        return self._game_files.get(path)

    # ------------------------------------------------------------------
    # 冲突检测
    # ------------------------------------------------------------------
    def conflict_detection(self, mods: Optional[List[Mod]] = None) -> List[Dict]:
        """检测模组间的文件冲突，区分冲突类型并给出解决方案。"""
        mods = list(mods) if mods is not None else list(self._mods.values())
        file_map: Dict[str, List[Tuple[str, str]]] = {}
        for m in mods:
            for path, change in m.file_changes.items():
                normalized = os.path.normpath(path).replace(os.sep, "/")
                file_map.setdefault(normalized, []).append((m.mod_id, change))

        conflicts: List[Dict] = []
        for path, entries in sorted(file_map.items()):
            if len(entries) < 2:
                continue
            change_types = {change for _, change in entries}
            if change_types == {self.CHANGE_OVERRIDE}:
                conflict_type = self.CONFLICT_OVERRIDE
            elif change_types == {self.CHANGE_MODIFY}:
                conflict_type = self.CONFLICT_MODIFY
            else:
                conflict_type = self.CONFLICT_MIXED
            conflicts.append({
                "file": path,
                "mods": [mod_id for mod_id, _ in entries],
                "type": conflict_type,
                "resolvable": True,
                "resolution": self._RESOLUTIONS[conflict_type],
            })
        return conflicts

    # ------------------------------------------------------------------
    # 模组卸载
    # ------------------------------------------------------------------
    def mod_uninstall(self, mod_id: str, confirm: bool = False) -> Dict:
        """卸载模组。必须 confirm=True 确认；卸载后还原游戏文件并清理模组文件。"""
        if mod_id not in self._mods:
            raise ModError(f"模组未安装: {mod_id}")
        if not confirm:
            raise UninstallConfirmationRequired(
                f"卸载模组 {mod_id} 需要玩家确认，请传入 confirm=True"
            )

        removed_files: List[str] = []
        restored_files: List[str] = []
        for (mid, path), backup in list(self._file_backups.items()):
            if mid != mod_id:
                continue
            if backup is None:
                self._game_files.pop(path, None)
                removed_files.append(path)
            else:
                self._game_files[path] = backup
                restored_files.append(path)
            del self._file_backups[(mid, path)]

        for path in self._installed_files.pop(mod_id, []):
            if path not in removed_files and path not in restored_files:
                removed_files.append(path)

        del self._mods[mod_id]
        self._load_order.remove(mod_id)
        self._state.pop(mod_id, None)
        self._mod_configs.pop(mod_id, None)
        self._save_configs()
        self._history.append(("uninstall", mod_id, time.time()))
        return {
            "mod_id": mod_id,
            "removed_files": removed_files,
            "restored_files": restored_files,
        }

    # ------------------------------------------------------------------
    # 版本检查
    # ------------------------------------------------------------------
    def _compatibility(self, min_v: Tuple[int, int, int],
                       max_v: Optional[Tuple[int, int, int]],
                       game: Tuple[int, int, int]) -> Tuple[bool, str]:
        if game < min_v:
            return False, "游戏版本低于模组要求的最低版本"
        if max_v is not None and game > max_v and game[0] != max_v[0]:
            return False, "模组不支持当前游戏主版本"
        return True, "兼容"

    def version_check(self, mod: Mod, game_version: Optional[str] = None,
                      auto_update: Optional[bool] = None) -> Dict:
        """检查模组与游戏版本兼容性；同主版本的小版本更新视为兼容。

        不兼容且存在可用更新时，按 auto_update（默认跟随管理器配置）自动更新模组。
        """
        gv_str = game_version or self.game_version
        game = self._parse_version(gv_str)
        do_update = self.auto_update if auto_update is None else auto_update

        result = {
            "mod_id": mod.mod_id,
            "mod_version": mod.version,
            "game_version": gv_str,
            "compatible": True,
            "reason": "兼容",
            "update_available": mod.update_info is not None,
            "updated": False,
        }

        min_v = self._parse_version(mod.min_game_version)
        max_v = self._parse_version(mod.max_game_version) if mod.max_game_version else None
        compatible, reason = self._compatibility(min_v, max_v, game)
        if compatible:
            return result

        result["compatible"] = False
        result["reason"] = reason

        if do_update and mod.update_info:
            new_min = self._parse_version(
                mod.update_info.get("min_game_version", mod.min_game_version))
            new_max_raw = mod.update_info.get("max_game_version", mod.max_game_version)
            new_max = self._parse_version(new_max_raw) if new_max_raw else None
            ok, _ = self._compatibility(new_min, new_max, game)
            if ok:
                mod.version = mod.update_info["version"]
                mod.min_game_version = mod.update_info.get(
                    "min_game_version", mod.min_game_version)
                mod.max_game_version = new_max_raw
                result.update({
                    "compatible": True,
                    "reason": f"已自动更新到 {mod.version} 以兼容游戏 {gv_str}",
                    "mod_version": mod.version,
                    "updated": True,
                })
                self._history.append(("auto_update", mod.mod_id, time.time()))
        return result

    # ------------------------------------------------------------------
    # 模组配置
    # ------------------------------------------------------------------
    def mod_config(self, mod_id: str, settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """读取或修改模组配置；修改会立即持久化保存。"""
        if mod_id not in self._mods:
            raise ModError(f"模组未安装: {mod_id}")
        cfg = self._mod_configs.setdefault(mod_id, dict(self._mods[mod_id].configs))
        if settings:
            cfg.update(settings)
            self._save_configs()
            self._history.append(("config", mod_id, time.time()))
        return dict(cfg)

    def config_ui(self, mod_id: str) -> Dict:
        """生成模组设置的文本界面，玩家可据此查看并修改配置。"""
        cfg = self.mod_config(mod_id)
        lines = [f"=== 模组设置: {mod_id} ==="]
        options = []
        for index, (key, value) in enumerate(cfg.items(), start=1):
            lines.append(f"  [{index}] {key}: {value}  (输入 {index} <新值> 修改)")
            options.append({"index": index, "key": key, "value": value})
        lines.append("  [s] 保存并退出")
        return {
            "mod_id": mod_id,
            "title": f"模组设置: {mod_id}",
            "options": options,
            "menu": "\n".join(lines),
        }

    # ------------------------------------------------------------------
    # 配置持久化
    # ------------------------------------------------------------------
    def _load_saved_configs(self):
        if not self._config_path or not os.path.exists(self._config_path):
            return
        try:
            with open(self._config_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return
        if isinstance(data, dict):
            for mod_id, cfg in data.items():
                if isinstance(cfg, dict):
                    self._mod_configs[mod_id] = cfg

    def _save_configs(self):
        if not self._config_path:
            return
        directory = os.path.dirname(self._config_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self._config_path, "w", encoding="utf-8") as fh:
            json.dump(self._mod_configs, fh, ensure_ascii=False, indent=2)
