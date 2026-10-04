"""
模组管理与加载系统 - 验收测试
运行方式：python -m pytest tests/test_mod_manager.py -q
共 15 个测试用例
"""
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mod.mod_manager import (
    ModManager,
    Mod,
    ModDependencyError,
    SandboxViolation,
    UninstallConfirmationRequired,
)


def make_mod(mod_id, **kwargs):
    kwargs.setdefault("name", mod_id)
    return Mod(mod_id=mod_id, **kwargs)


class TestModManager:
    def setup_method(self):
        self.system = ModManager()

    # ---------------- 模组加载 ----------------

    def test_case_01(self):
        """加载模组应完整加载脚本、资源、配置、本地化"""
        mod = make_mod(
            "demo",
            scripts={"mods/demo/main.py": "print('hi')"},
            resources={"mods/demo/tex.png": "PNG"},
            config_files={"mods/demo/settings.cfg": "volume=5"},
            localizations={"mods/demo/zh_CN.lang": "你好"},
        )
        entry = self.system.load_mod(mod)
        assert entry["scripts"] == {"mods/demo/main.py": "print('hi')"}
        assert entry["resources"] == {"mods/demo/tex.png": "PNG"}
        assert "mods/demo/settings.cfg" in entry["configs"]
        assert entry["localizations"] == {"mods/demo/zh_CN.lang": "你好"}

    def test_case_02(self):
        """批量加载应按依赖拓扑顺序加载，缺失依赖应报错"""
        a = make_mod("a")
        b = make_mod("b", dependencies=["a"])
        c = make_mod("c", dependencies=["b"])
        order = self.system.load_mods([c, b, a])
        assert order == ["a", "b", "c"]
        assert self.system.load_order == ["a", "b", "c"]
        with pytest.raises(ModDependencyError):
            ModManager().load_mods([make_mod("x", dependencies=["ghost"])])

    def test_case_03(self):
        """加载应在沙箱内进行，禁止访问模组目录之外的文件"""
        with pytest.raises(SandboxViolation):
            self.system.load_mod(make_mod("evil", scripts={"../../etc/passwd": "x"}))
        with pytest.raises(SandboxViolation):
            self.system.load_mod(make_mod("evil2", resources={"/game/core.dll": "x"}))
        ok = make_mod("good", scripts={"mods/good/a.py": "1"},
                      resources={"shared/lib.dat": "2"})
        entry = self.system.load_mod(ok)
        assert "mods/good/a.py" in entry["scripts"]
        assert "shared/lib.dat" in entry["resources"]

    # ---------------- 冲突检测 ----------------

    def test_case_04(self):
        """冲突检测应发现修改同一文件的模组"""
        m1 = make_mod("m1", file_changes={"game_data/w.json": "override"})
        m2 = make_mod("m2", file_changes={"game_data/w.json": "override"})
        m3 = make_mod("m3", file_changes={"game_data/other.json": "override"})
        conflicts = self.system.conflict_detection([m1, m2, m3])
        assert len(conflicts) == 1
        assert conflicts[0]["file"] == "game_data/w.json"
        assert sorted(conflicts[0]["mods"]) == ["m1", "m2"]

    def test_case_05(self):
        """冲突检测应区分覆盖、修改、混合三种冲突类型"""
        m1 = make_mod("m1", file_changes={"f1": "override", "f2": "modify", "f3": "override"})
        m2 = make_mod("m2", file_changes={"f1": "override", "f2": "modify", "f3": "modify"})
        conflicts = {c["file"]: c["type"] for c in self.system.conflict_detection([m1, m2])}
        assert conflicts["f1"] == "override"
        assert conflicts["f2"] == "modify"
        assert conflicts["f3"] == "mixed"

    def test_case_06(self):
        """每个冲突都应附带可执行的解决方案"""
        m1 = make_mod("m1", file_changes={"f1": "override", "f2": "modify"})
        m2 = make_mod("m2", file_changes={"f1": "override", "f2": "modify"})
        conflicts = self.system.conflict_detection([m1, m2])
        assert conflicts
        for conflict in conflicts:
            assert conflict["resolvable"] is True
            assert isinstance(conflict["resolution"], str)
            assert len(conflict["resolution"]) > 0

    # ---------------- 模组卸载 ----------------

    def test_case_07(self):
        """卸载后模组应从管理器中移除"""
        self.system.load_mod(make_mod("u1", scripts={"mods/u1/a.py": "1"}))
        assert "u1" in self.system.load_order
        result = self.system.mod_uninstall("u1", confirm=True)
        assert result["mod_id"] == "u1"
        assert "u1" not in self.system.load_order
        assert "mods/u1/a.py" in result["removed_files"]

    def test_case_08(self):
        """卸载应干净还原模组改动过的游戏文件"""
        mgr = ModManager()
        mgr._game_files["game_data/w.json"] = "original"
        mod = make_mod("u2", file_changes={"game_data/w.json": "override",
                                           "game_data/new.json": "override"})
        mgr.load_mod(mod)
        assert mgr.get_game_file("game_data/w.json") != "original"
        result = mgr.mod_uninstall("u2", confirm=True)
        assert mgr.get_game_file("game_data/w.json") == "original"
        assert mgr.get_game_file("game_data/new.json") is None
        assert "game_data/w.json" in result["restored_files"]

    def test_case_09(self):
        """卸载必须经玩家确认，未确认时不得移除任何内容"""
        self.system.load_mod(make_mod("u3", file_changes={"game_data/x.json": "override"}))
        with pytest.raises(UninstallConfirmationRequired):
            self.system.mod_uninstall("u3")
        assert "u3" in self.system.load_order
        assert self.system.get_game_file("game_data/x.json") is not None

    # ---------------- 版本检查 ----------------

    def test_case_10(self):
        """版本检查应拒绝与游戏主版本不兼容的旧模组"""
        mgr = ModManager({"game_version": "2.0.0", "auto_update": False})
        mod = make_mod("v1", version="1.0.0", min_game_version="1.0.0",
                       max_game_version="1.9.0")
        result = mgr.version_check(mod)
        assert result["compatible"] is False
        assert result["updated"] is False
        too_new = mgr.version_check(
            make_mod("v2", min_game_version="3.0.0"), game_version="2.5.0")
        assert too_new["compatible"] is False

    def test_case_11(self):
        """同主版本的小版本/补丁更新应判定为兼容"""
        mgr = ModManager({"game_version": "1.9.9"})
        mod = make_mod("v3", min_game_version="1.0.0", max_game_version="1.5.0")
        result = mgr.version_check(mod)
        assert result["compatible"] is True
        assert result["reason"] == "兼容"

    def test_case_12(self):
        """不兼容的旧模组存在更新时应自动更新到兼容版本"""
        mgr = ModManager({"game_version": "2.1.0", "auto_update": True})
        mod = make_mod(
            "v4", version="1.0.0", min_game_version="1.0.0", max_game_version="1.9.0",
            update_info={"version": "2.0.0", "min_game_version": "2.0.0",
                         "max_game_version": "2.9.0"},
        )
        result = mgr.version_check(mod)
        assert result["update_available"] is True
        assert result["updated"] is True
        assert result["compatible"] is True
        assert mod.version == "2.0.0"

    # ---------------- 模组配置 ----------------

    def test_case_13(self):
        """玩家应能通过 mod_config 读取并调整模组设置"""
        self.system.load_mod(make_mod("c1", configs={"volume": 5, "difficulty": "normal"}))
        cfg = self.system.mod_config("c1")
        assert cfg["volume"] == 5
        updated = self.system.mod_config("c1", {"volume": 9})
        assert updated["volume"] == 9
        assert self.system.mod_config("c1")["volume"] == 9

    def test_case_14(self, tmp_path=None):
        """配置应持久化保存，重启（新建管理器）后仍然生效"""
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "mod_configs.json")
        mgr1 = ModManager({"config_path": path})
        mgr1.load_mod(make_mod("c2", configs={"volume": 5}))
        mgr1.mod_config("c2", {"volume": 8})
        assert os.path.exists(path)
        mgr2 = ModManager({"config_path": path})
        mgr2.load_mod(make_mod("c2", configs={"volume": 5}))
        assert mgr2.mod_config("c2")["volume"] == 8

    def test_case_15(self):
        """配置应提供设置界面，且界面中的修改能生效"""
        self.system.load_mod(make_mod("c3", configs={"volume": 5, "fps": 60}))
        ui = self.system.config_ui("c3")
        assert ui["title"].startswith("模组设置")
        keys = [opt["key"] for opt in ui["options"]]
        assert keys == ["volume", "fps"]
        assert "volume" in ui["menu"] and "fps" in ui["menu"]
        self.system.mod_config("c3", {"fps": 144})
        ui2 = self.system.config_ui("c3")
        assert ui2["options"][1]["value"] == 144


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
