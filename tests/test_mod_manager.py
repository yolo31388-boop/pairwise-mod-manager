"""
模组管理与加载系统 - 验收测试
运行方式：python -m pytest tests/test_mod_manager.py -q
共 15 个测试用例
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mod.mod_manager import ModManager, SandboxViolation


class TestModManager:
    def setup_method(self):
        self.system = ModManager()

    # ------------------------------------------------------------------
    # 模组加载：完整加载
    # ------------------------------------------------------------------
    def test_case_01(self):
        """load_mod 完整加载脚本、资源、配置、本地化。"""
        mod = {
            "id": "hello",
            "scripts": ["main.lua", "util.lua"],
            "resources": {"textures/icon.png": "PNGDATA"},
            "config": {"volume": 5},
            "localization": {"zh": {"hello": "你好"}, "en": {"hello": "Hello"}},
        }
        result = self.system.load_mod(mod)
        assert result["scripts"] == ["main.lua", "util.lua"]
        assert result["resources"] == {"textures/icon.png": "PNGDATA"}
        assert result["config"]["volume"] == 5
        assert result["localization"]["zh"]["hello"] == "你好"
        assert result["localization"]["en"]["hello"] == "Hello"

    def test_case_02(self):
        """load_mods 按依赖关系排序加载，依赖先加载。"""
        core = {"id": "core"}
        ui = {"id": "ui", "dependencies": ["core"]}
        addon = {"id": "addon", "dependencies": ["ui"]}
        order = self.system.load_mods([addon, ui, core])
        assert order == ["core", "ui", "addon"]
        assert set(self.system._loaded) == {"core", "ui", "addon"}

    def test_case_03(self, tmp_path):
        """模组加载有沙箱，不能访问沙箱目录之外的文件。"""
        mgr = ModManager(
            {"game_dir": str(tmp_path), "mods_dir": str(tmp_path / "mods")}
        )
        mgr.install_mod({"id": "evil"})
        inside = mgr.mod_access_file("evil", "data/config.ini")
        assert inside.startswith(os.path.join(str(tmp_path), "mods", "evil"))
        with pytest.raises(SandboxViolation):
            mgr.mod_access_file("evil", "../../saves/player.dat")
        with pytest.raises(SandboxViolation):
            mgr.mod_access_file("evil", os.path.join(str(tmp_path), "secret.dll"))

    # ------------------------------------------------------------------
    # 冲突检测
    # ------------------------------------------------------------------
    def test_case_04(self):
        """conflict_detection 能检测两个模组修改同一个文件。"""
        mod_a = {"id": "a", "modifies": {"game/main.lua": "modify"}}
        mod_b = {"id": "b", "modifies": {"game/main.lua": "modify"}}
        mod_c = {"id": "c", "modifies": {"game/other.lua": "modify"}}
        conflicts = self.system.conflict_detection([mod_a, mod_b, mod_c])
        assert len(conflicts) == 1
        assert conflicts[0]["file"] == "game/main.lua"
        assert set(conflicts[0]["mods"]) == {"a", "b"}

    def test_case_05(self):
        """冲突检测区分冲突类型：覆盖(overwrite)与修改(modify)。"""
        mod_a = {"id": "a", "modifies": {"data/core.dat": "overwrite"}}
        mod_b = {"id": "b", "modifies": {"data/core.dat": "overwrite"}}
        mod_c = {"id": "c", "modifies": {"data/ui.dat": "modify"}}
        mod_d = {"id": "d", "modifies": {"data/ui.dat": "modify"}}
        conflicts = self.system.conflict_detection([mod_a, mod_b, mod_c, mod_d])
        by_file = {c["file"]: c for c in conflicts}
        assert by_file["data/core.dat"]["type"] == "overwrite"
        assert by_file["data/ui.dat"]["type"] == "modify"

    def test_case_06(self):
        """每个冲突都附带解决方案，玩家知道如何处理。"""
        mod_a = {"id": "a", "modifies": {"x.lua": "overwrite"}}
        mod_b = {"id": "b", "modifies": {"x.lua": "overwrite"}}
        conflicts = self.system.conflict_detection([mod_a, mod_b])
        assert len(conflicts) == 1
        resolutions = conflicts[0]["resolutions"]
        assert len(resolutions) >= 2
        actions = {r["action"] for r in resolutions}
        assert "disable_mod" in actions
        assert all(r.get("description") for r in resolutions)

    # ------------------------------------------------------------------
    # 模组卸载
    # ------------------------------------------------------------------
    def test_case_07(self):
        """卸载需要确认：未确认时不卸载。"""
        self.system.install_mod({"id": "m1"})
        result = self.system.mod_uninstall("m1")
        assert result["status"] == "confirmation_required"
        assert "m1" in self.system._installed

    def test_case_08(self, tmp_path):
        """确认卸载后模组文件被清理干净，其他文件不受影响。"""
        mgr = ModManager({"game_dir": str(tmp_path)})
        mgr.install_mod(
            {
                "id": "m1",
                "files": ["mods_data/m1/a.dat", "mods_data/m1/b.dat"],
                "resources": {"mods_data/m1/a.dat": "A", "mods_data/m1/b.dat": "B"},
            }
        )
        keep = tmp_path / "mods_data" / "keep.dat"
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.write_text("KEEP", encoding="utf-8")
        result = mgr.mod_uninstall("m1", confirm=True)
        assert result["status"] == "uninstalled"
        assert set(result["removed_files"]) == {
            "mods_data/m1/a.dat",
            "mods_data/m1/b.dat",
        }
        assert not (tmp_path / "mods_data" / "m1" / "a.dat").exists()
        assert not (tmp_path / "mods_data" / "m1" / "b.dat").exists()
        assert keep.exists()

    def test_case_09(self):
        """确认卸载后模组从已安装/已加载列表移除，重复卸载报错。"""
        self.system.install_mod({"id": "m1"})
        self.system.load_mod({"id": "m1"})
        result = self.system.mod_uninstall("m1", confirm=True)
        assert result["status"] == "uninstalled"
        assert "m1" not in self.system._installed
        assert "m1" not in self.system._loaded
        with pytest.raises(KeyError):
            self.system.mod_uninstall("m1", confirm=True)

    # ------------------------------------------------------------------
    # 版本检查
    # ------------------------------------------------------------------
    def test_case_10(self):
        """version_check 实际生效：旧主版本模组与新主版本游戏不兼容。"""
        mod = {"id": "old", "version": "1.0.0", "max_game_version": "1.9.9"}
        result = self.system.version_check(mod, "2.0.0")
        assert result["compatible"] is False
        assert result["updated"] is False
        assert result["reason"]

    def test_case_11(self):
        """小版本更新视为兼容，不误报不兼容。"""
        mod = {"id": "m", "version": "1.2.0", "max_game_version": "1.2.0"}
        result = self.system.version_check(mod, "1.5.3")
        assert result["compatible"] is True
        mod_below_min = {"id": "m2", "min_game_version": "2.0.0"}
        result2 = self.system.version_check(mod_below_min, "1.5.3")
        assert result2["compatible"] is False

    def test_case_12(self):
        """不兼容的旧模组有可用新版本时自动更新。"""
        mod = {
            "id": "up",
            "version": "1.0.0",
            "max_game_version": "1.9.9",
            "latest_version": "2.1.0",
            "latest_min_game_version": "2.0.0",
            "latest_max_game_version": "2.9.9",
        }
        result = self.system.version_check(mod, "2.3.0")
        assert result["compatible"] is True
        assert result["updated"] is True
        assert result["mod_version"] == "2.1.0"

    # ------------------------------------------------------------------
    # 模组配置
    # ------------------------------------------------------------------
    def test_case_13(self):
        """mod_config 支持读取和修改模组设置。"""
        self.system.install_mod({"id": "m1", "config": {"volume": 5, "fps": 60}})
        assert self.system.mod_config("m1", "volume") == 5
        self.system.mod_config("m1", "volume", 8)
        assert self.system.mod_config("m1", "volume") == 8
        assert self.system.mod_config("m1")["fps"] == 60

    def test_case_14(self, tmp_path):
        """配置持久化保存，重启（新实例）后仍然保留。"""
        config_path = str(tmp_path / "mod_configs.json")
        mgr1 = ModManager({"config_path": config_path})
        mgr1.install_mod({"id": "m1", "config": {"volume": 5}})
        mgr1.mod_config("m1", "volume", 9)
        assert os.path.exists(config_path)
        mgr2 = ModManager({"config_path": config_path})
        mgr2.install_mod({"id": "m1", "config": {"volume": 5}})
        assert mgr2.mod_config("m1", "volume") == 9
        with open(config_path, encoding="utf-8") as fh:
            assert json.load(fh)["m1"]["volume"] == 9

    def test_case_15(self):
        """配置有界面描述：根据配置模式生成控件列表。"""
        self.system.install_mod(
            {
                "id": "m1",
                "name": "画质增强",
                "config": {"volume": 5, "hd": True},
                "config_schema": {
                    "volume": {"label": "音量", "min": 0, "max": 10},
                    "hd": {"label": "高清材质"},
                },
            }
        )
        ui = self.system.get_config_ui("m1")
        assert ui["title"] == "画质增强"
        widgets = {w["key"]: w for w in ui["widgets"]}
        assert widgets["volume"]["widget"] == "slider"
        assert widgets["volume"]["min"] == 0
        assert widgets["volume"]["max"] == 10
        assert widgets["volume"]["value"] == 5
        assert widgets["hd"]["widget"] == "checkbox"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
