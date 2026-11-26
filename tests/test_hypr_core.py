import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hypr_core  # noqa: E402
import plugins_core as core  # noqa: E402


def _hyprctl_ok(argv, **_kwargs):
    """Default mock for plugins_core.run returning clean reload/configerrors."""
    if argv[:2] == ["hyprctl", "reload"]:
        return core.Result(0, "ok\n")
    if argv[:2] == ["hyprctl", "configerrors"]:
        return core.Result(0, "\n")
    return core.Result(1, "", f"unexpected command: {argv}")


class _HyprTempMixin:
    """Redirect HYPR_DIR and STATE_FILE into an isolated temporary directory per test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.hypr_dir = self.root / "hypr"
        self.hypr_dir.mkdir(mode=0o755)
        self.state_dir = self.root / "state"
        self.state_file = self.state_dir / "hypr-state.json"
        self._p1 = mock.patch.object(hypr_core, "HYPR_DIR", self.hypr_dir)
        self._p2 = mock.patch.object(hypr_core, "STATE_FILE", self.state_file)
        self._p1.start()
        self._p2.start()

    def tearDown(self):
        self._p2.stop()
        self._p1.stop()
        self._tmp.cleanup()


class RenderLuaTest(unittest.TestCase):
    def test_escape_lua_string_strips_controls_and_escapes_quotes_backslashes(self):
        self.assertEqual(hypr_core.escape_lua_string('a"b\\c'), '"a\\"b\\\\c"')
        self.assertEqual(hypr_core.escape_lua_string("line1\r\nline2\x00\x1b\x7f"), '"line1line2"')

    def test_render_monitors_enabled_disabled_mirror_hdr_vrr(self):
        state = {
            "monitors": {
                "HDMI-A-1": {
                    "enabled": True,
                    "mode": "3840x2160@120.00Hz",
                    "position": "0x0",
                    "scale": 1.5,
                    "transform": 0,
                    "vrr": 2,
                    "hdr": True,
                },
                "DP-1": {
                    "enabled": False,
                    "mode": "1920x1080@60",
                },
                "eDP-1": {
                    "enabled": True,
                    "mode": "preferred",
                    "position": "auto-right",
                    "scale": "auto",
                    "vrr": -1,
                    "mirror": "HDMI-A-1",
                },
            },
            "input": {},
            "animations": {},
        }
        lua = hypr_core.render_lua(state)
        self.assertIn('output = "DP-1",\n    disabled = true,', lua)
        self.assertIn('output = "HDMI-A-1",', lua)
        self.assertIn('mode = "3840x2160@120.00",', lua)
        self.assertIn('position = "0x0",', lua)
        self.assertIn("scale = 1.5,", lua)
        self.assertIn("transform = 0,", lua)
        self.assertIn("vrr = 2,", lua)
        self.assertIn("bitdepth = 10,", lua)
        self.assertIn('cm = "hdr",', lua)
        self.assertIn('output = "eDP-1",', lua)
        self.assertIn('scale = "auto",', lua)
        self.assertIn('mirror = "HDMI-A-1",', lua)
        self.assertNotIn("vrr = -1", lua)

    def test_render_input_dual_layout_caps_compose_and_touchpad(self):
        state = {
            "monitors": {},
            "input": {
                "kb_layout": "us",
                "kb_layout2": "de",
                "caps_option": "caps:escape",
                "compose_option": "compose:ralt",
                "repeat_delay": 250,
                "repeat_rate": 40,
                "numlock_by_default": True,
                "left_handed": False,
                "sensitivity": -0.25,
                "accel_profile": "flat",
                "mouse_scroll_factor": 1.5,
                "mouse_natural_scroll": False,
                "tp_disable_while_typing": True,
                "tp_tap_to_click": True,
                "tp_tap_and_drag": True,
                "tp_drag_lock": True,
                "tp_clickfinger": False,
                "tp_scroll_factor": 0.4,
                "tp_natural_scroll": True,
            },
            "animations": {},
        }
        lua = hypr_core.render_lua(state)
        self.assertIn('kb_layout = "us,de",', lua)
        self.assertIn('kb_options = "grp:alt_shift_toggle,caps:escape,compose:ralt",', lua)
        self.assertIn("repeat_delay = 250,", lua)
        self.assertIn("repeat_rate = 40,", lua)
        self.assertIn("numlock_by_default = true,", lua)
        self.assertIn("left_handed = false,", lua)
        self.assertIn("sensitivity = -0.25,", lua)
        self.assertIn('accel_profile = "flat",', lua)
        self.assertIn("scroll_factor = 1.5,", lua)
        self.assertIn("natural_scroll = false,", lua)
        self.assertIn("touchpad = {", lua)
        self.assertIn("disable_while_typing = true,", lua)
        self.assertIn("tap_to_click = true,", lua)
        self.assertIn("tap_and_drag = true,", lua)
        self.assertIn("drag_lock = 1,", lua)
        self.assertIn("clickfinger_behavior = false,", lua)
        self.assertIn("scroll_factor = 0.4,", lua)
        self.assertIn("natural_scroll = true,", lua)

    def test_render_input_single_layout_omits_grp_toggle(self):
        state = {
            "monitors": {},
            "input": {
                "kb_layout": "de",
                "kb_layout2": "",
                "caps_option": "caps:ctrl_modifier",
                "compose_option": "",
            },
            "animations": {},
        }
        lua = hypr_core.render_lua(state)
        self.assertIn('kb_layout = "de",', lua)
        self.assertIn('kb_options = "caps:ctrl_modifier",', lua)
        self.assertNotIn("grp:alt_shift_toggle", lua)

    def test_render_animations_popin_and_workspaces_and_layers(self):
        state = {
            "monitors": {},
            "input": {},
            "animations": {
                "enabled": True,
                "windows_enabled": True,
                "windows_style": "popin",
                "windows_popin": 85,
                "windows_speed": 4.5,
                "workspaces_enabled": True,
                "workspaces_style": "slidefadevert",
                "workspaces_speed": 6.0,
                "layers_enabled": False,
            },
        }
        lua = hypr_core.render_lua(state)
        self.assertIn("animations = {\n        enabled = true,\n    },", lua)
        self.assertIn('leaf = "windowsIn", enabled = true, speed = 4.5, bezier = "default", style = "popin 85%"', lua)
        self.assertIn('leaf = "windowsMove", enabled = true, speed = 4.5, bezier = "default" })', lua)
        self.assertIn('leaf = "workspaces", enabled = true, speed = 6, bezier = "default", style = "slidefadevert"', lua)
        self.assertIn('leaf = "layersIn", enabled = false, speed = 5, bezier = "default"', lua)
        self.assertIn('leaf = "fadeLayersOut", enabled = false, speed = 5, bezier = "default"', lua)

    def test_render_combined_multi_section_state(self):
        state = {
            "monitors": {"DP-2": {"enabled": True, "mode": "2560x1440@144.00", "position": "auto", "scale": 1.0}},
            "input": {"kb_layout": "us", "repeat_delay": 300},
            "animations": {"enabled": False, "layers_enabled": False},
        }
        lua = hypr_core.render_lua(state)
        self.assertIn('output = "DP-2",', lua)
        self.assertIn('kb_layout = "us",', lua)
        self.assertIn("animations = {\n        enabled = false,\n    },", lua)
        # Deterministic output across calls
        self.assertEqual(lua, hypr_core.render_lua(state))


class HookAndPersistenceTest(_HyprTempMixin, unittest.TestCase):
    def test_read_never_touches_hyprland_lua(self):
        hyprland = self.hypr_dir / "hyprland.lua"
        hyprland.write_text("-- original config\n")
        before = hyprland.read_text()

        def fake_run(argv, **_kw):
            if argv[:3] == ["hyprctl", "monitors", "all"]:
                return core.Result(0, "[]")
            if argv[:2] == ["hyprctl", "devices"]:
                return core.Result(0, '{"mice": [], "keyboards": []}')
            if argv[:2] == ["hyprctl", "getoption"]:
                return core.Result(1, "", "no hyprland")
            if argv[:2] == ["hyprctl", "animations"]:
                return core.Result(1, "", "no hyprland")
            return core.Result(1, "", "unexpected")

        with mock.patch.object(core, "run", side_effect=fake_run):
            hypr_core.read_managed_state()
            hypr_core.list_monitors()
            hypr_core.read_input()
            hypr_core.read_animations()

        self.assertEqual(hyprland.read_text(), before)
        self.assertFalse((self.hypr_dir / hypr_core.SETTINGS_LUA_NAME).exists())

    def test_hook_idempotency_and_clean_removal_on_reset_all(self):
        hyprland = self.hypr_dir / "hyprland.lua"
        original = "-- user hyprland.lua\nrequire(\"hyprland.general\")\n"
        hyprland.write_text(original)

        with mock.patch.object(core, "run", side_effect=_hyprctl_ok):
            ok1, _ = hypr_core.save_input({"kb_layout": "de"})
            ok2, _ = hypr_core.save_animations({"enabled": True, "windows_style": "slide"})
            ok3, _ = hypr_core.apply_monitor("HDMI-A-1", {"enabled": True, "mode": "3840x2160@60"})

        self.assertTrue(ok1 and ok2 and ok3)
        text = hyprland.read_text()
        self.assertEqual(text.count(hypr_core.HOOK_MARKER), 1)
        self.assertIn(hypr_core.HOOK_LINE, text)

        lua_file = self.hypr_dir / hypr_core.SETTINGS_LUA_NAME
        self.assertTrue(lua_file.exists())
        self.assertEqual(lua_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state_file.stat().st_mode & 0o777, 0o600)

        # Full reset removes hook and settings-manager.lua cleanly
        with mock.patch.object(core, "run", side_effect=_hyprctl_ok):
            ok_reset, _ = hypr_core.reset_all()
        self.assertTrue(ok_reset)
        self.assertEqual(hyprland.read_text(), original)
        self.assertFalse(lua_file.exists())
        self.assertEqual(hypr_core.read_managed_state(), {"monitors": {}, "input": {}, "animations": {}})

    def test_write_without_hyprland_lua_does_not_create_bare_hyprland_lua(self):
        with mock.patch.object(core, "run", side_effect=_hyprctl_ok):
            ok, _ = hypr_core.save_input({"kb_layout": "us"})
        self.assertTrue(ok)
        self.assertTrue((self.hypr_dir / hypr_core.SETTINGS_LUA_NAME).exists())
        self.assertFalse((self.hypr_dir / "hyprland.lua").exists())

    def test_configerrors_reported_as_failure(self):
        def fake_err(argv, **_kw):
            if argv[:2] == ["hyprctl", "reload"]:
                return core.Result(0, "ok\n")
            if argv[:2] == ["hyprctl", "configerrors"]:
                return core.Result(0, "config error at line 3\n")
            return core.Result(1, "", "unexpected")

        with mock.patch.object(core, "run", side_effect=fake_err):
            ok, msg = hypr_core.save_input({"kb_layout": "us"})
        self.assertFalse(ok)
        self.assertIn("config error at line 3", msg)

    def test_sections_compose_without_losing_each_other(self):
        with mock.patch.object(core, "run", side_effect=_hyprctl_ok):
            self.assertTrue(hypr_core.apply_monitor("DP-1", {"enabled": True, "scale": 1.25})[0])
            self.assertTrue(hypr_core.save_input({"kb_layout": "us", "kb_layout2": "de"})[0])
            self.assertTrue(hypr_core.save_animations({"enabled": True, "windows_style": "gnomed"})[0])

        lua = (self.hypr_dir / hypr_core.SETTINGS_LUA_NAME).read_text()
        self.assertIn('output = "DP-1",', lua)
        self.assertIn('kb_layout = "us,de",', lua)
        self.assertIn('style = "gnomed"', lua)

        # Resetting input keeps monitors and animations intact
        with mock.patch.object(core, "run", side_effect=_hyprctl_ok):
            self.assertTrue(hypr_core.reset_input()[0])
        lua_after = (self.hypr_dir / hypr_core.SETTINGS_LUA_NAME).read_text()
        self.assertIn('output = "DP-1",', lua_after)
        self.assertIn('style = "gnomed"', lua_after)
        self.assertNotIn("kb_layout", lua_after)


class DisplaysRevertAndReadTest(_HyprTempMixin, unittest.TestCase):
    def test_snapshot_apply_restore_revert_flow(self):
        with mock.patch.object(core, "run", side_effect=_hyprctl_ok) as run_mock:
            # Start with an initial saved monitor and an input setting
            self.assertTrue(hypr_core.save_input({"kb_layout": "de"})[0])
            self.assertTrue(
                hypr_core.apply_monitor(
                    "HDMI-A-1",
                    {"enabled": True, "mode": "3840x2160@60.00", "scale": 1.0, "position": "0x0"},
                )[0]
            )

            snap = hypr_core.snapshot_state()
            # Apply a risky change (e.g. new mode & scale)
            ok_apply, _ = hypr_core.apply_monitor(
                "HDMI-A-1",
                {"enabled": True, "mode": "3840x2160@120.00", "scale": 2.0, "transform": 1, "vrr": 1},
            )
            self.assertTrue(ok_apply)
            changed_lua = (self.hypr_dir / hypr_core.SETTINGS_LUA_NAME).read_text()
            self.assertIn('mode = "3840x2160@120.00",', changed_lua)
            self.assertIn("scale = 2,", changed_lua)
            self.assertIn("transform = 1,", changed_lua)

            # 15-second timer expires or user clicks Revert
            ok_restore, _ = hypr_core.restore_state(snap)
            self.assertTrue(ok_restore)
            reverted_lua = (self.hypr_dir / hypr_core.SETTINGS_LUA_NAME).read_text()
            self.assertIn('mode = "3840x2160@60.00",', reverted_lua)
            self.assertIn("scale = 1,", reverted_lua)
            self.assertNotIn("transform = 1,", reverted_lua)
            self.assertIn('kb_layout = "de",', reverted_lua)

        # Verify hyprctl reload + configerrors ran on each write
        cmds = [c.args[0][:2] for c in run_mock.call_args_list]
        self.assertGreaterEqual(cmds.count(["hyprctl", "reload"]), 4)
        self.assertGreaterEqual(cmds.count(["hyprctl", "configerrors"]), 4)

    def test_list_monitors_parses_dedupes_sorts_and_merges_saved(self):
        with mock.patch.object(core, "run", side_effect=_hyprctl_ok):
            hypr_core.apply_monitor("HDMI-A-1", {"enabled": True, "vrr": 2, "hdr": True, "position": "auto-right"})

        sample = [
            {
                "name": "HDMI-A-1",
                "description": "LG TV\x1b[31m\nTest",
                "width": 3840,
                "height": 2160,
                "refreshRate": 120.0,
                "availableModes": [
                    "1920x1080@60.00Hz",
                    "3840x2160@60.00Hz",
                    "3840x2160@120.00Hz",
                    "3840x2160@60.00Hz",  # duplicate
                    "bad;mode",
                ],
                "x": 0,
                "y": 0,
                "scale": 1.5,
                "transform": 0,
                "vrr": False,
                "disabled": False,
                "mirrorOf": "none",
            }
        ]
        with mock.patch.object(core, "run", return_value=core.Result(0, json.dumps(sample))):
            res = hypr_core.list_monitors()

        self.assertTrue(res["ok"])
        self.assertEqual(len(res["monitors"]), 1)
        mon = res["monitors"][0]
        self.assertEqual(mon["name"], "HDMI-A-1")
        self.assertNotIn("\n", mon["description"])
        self.assertNotIn("\x1b", mon["description"])
        self.assertEqual(mon["available_modes"], ["3840x2160@120.00", "3840x2160@60.00", "1920x1080@60.00"])
        self.assertEqual(mon["vrr"], 2)
        self.assertTrue(mon["hdr"])
        self.assertEqual(mon["position"], "auto-right")
        self.assertEqual(mon["mirror"], "")


class InputAndAnimationsReadTest(_HyprTempMixin, unittest.TestCase):
    def test_read_input_detects_touchpad_and_reads_live_options(self):
        devices = {
            "mice": [
                {"name": "logitech-usb-receiver-mouse"},
                {"name": "elan0676:00-04f3:3195-touchpad"},
            ],
            "keyboards": [
                {"name": "at-translated-set-2-keyboard", "layout": "de,us", "options": "grp:alt_shift_toggle,caps:escape", "main": True},
            ],
        }
        options = {
            "input:kb_layout": {"str": "de,us"},
            "input:kb_options": {"str": "grp:alt_shift_toggle,caps:escape,compose:ralt"},
            "input:repeat_delay": {"int": 300},
            "input:repeat_rate": {"int": 50},
            "input:numlock_by_default": {"bool": True},
            "input:left_handed": {"bool": False},
            "input:sensitivity": {"float": 0.2},
            "input:accel_profile": {"str": "flat"},
            "input:scroll_factor": {"float": 1.25},
            "input:natural_scroll": {"bool": False},
            "input:touchpad:disable_while_typing": {"bool": True},
            "input:touchpad:tap_to_click": {"bool": True},
            "input:touchpad:tap_and_drag": {"bool": False},
            "input:touchpad:drag_lock": {"int": 1},
            "input:touchpad:clickfinger_behavior": {"bool": True},
            "input:touchpad:scroll_factor": {"float": 0.5},
            "input:touchpad:natural_scroll": {"bool": True},
        }

        def fake_run(argv, **_kw):
            if argv[:2] == ["hyprctl", "devices"]:
                return core.Result(0, json.dumps(devices))
            if argv[:2] == ["hyprctl", "getoption"]:
                opt = argv[2]
                return core.Result(0, json.dumps(options[opt])) if opt in options else core.Result(1, "")
            return core.Result(1, "")

        with mock.patch.object(core, "run", side_effect=fake_run):
            data = hypr_core.read_input()

        self.assertTrue(data["has_touchpad"])
        self.assertEqual(data["kb_layout"], "de")
        self.assertEqual(data["kb_layout2"], "us")
        self.assertEqual(data["caps_option"], "caps:escape")
        self.assertEqual(data["compose_option"], "compose:ralt")
        self.assertEqual(data["repeat_delay"], 300)
        self.assertEqual(data["repeat_rate"], 50)
        self.assertTrue(data["numlock_by_default"])
        self.assertEqual(data["sensitivity"], 0.2)
        self.assertEqual(data["accel_profile"], "flat")
        self.assertTrue(data["tp_drag_lock"])
        self.assertTrue(data["tp_clickfinger"])
        self.assertEqual(data["tp_scroll_factor"], 0.5)

    def test_read_animations_live_and_saved_customized_flag(self):
        live_anim = [[
            {"name": "windowsIn", "overridden": True, "enabled": True, "speed": 4.0, "style": "popin 75%"},
            {"name": "workspaces", "overridden": True, "enabled": True, "speed": 6.0, "style": "fade"},
            {"name": "layersIn", "overridden": True, "enabled": False, "speed": 5.0, "style": "slide"},
        ]]

        def fake_run(argv, **_kw):
            if argv[:3] == ["hyprctl", "getoption", "animations:enabled"]:
                return core.Result(0, json.dumps({"option": "animations:enabled", "bool": True}))
            if argv[:2] == ["hyprctl", "animations"]:
                return core.Result(0, json.dumps(live_anim))
            return _hyprctl_ok(argv)

        with mock.patch.object(core, "run", side_effect=fake_run):
            before = hypr_core.read_animations()
            self.assertFalse(before["customized"])
            self.assertEqual(before["windows_style"], "popin")
            self.assertEqual(before["windows_popin"], 75)
            self.assertEqual(before["windows_speed"], 4.0)
            self.assertEqual(before["workspaces_style"], "fade")
            self.assertFalse(before["layers_enabled"])

            ok, _ = hypr_core.save_animations({**before, "windows_style": "gnomed"})
            self.assertTrue(ok)
            after = hypr_core.read_animations()
            self.assertTrue(after["customized"])
            self.assertEqual(after["windows_style"], "gnomed")

            ok_r, _ = hypr_core.reset_animations()
            self.assertTrue(ok_r)
            reset = hypr_core.read_animations()
            self.assertFalse(reset["customized"])


class HostileInputTest(_HyprTempMixin, unittest.TestCase):
    def test_hostile_monitor_outputs_rejected_without_running_commands(self):
        bad_outputs = [
            "",
            "HDMI-A-1\n",
            "HDMI-A-1\r",
            'HDMI-1"\nos.execute("id")',
            "HDMI-1\r\nfoo",
            "$(id)",
            "DP-1;rm -rf /",
            "DP 1",
            "a\\b",
            "../etc/passwd",
            "X" * 65,
        ]
        with mock.patch.object(core, "run") as run_mock:
            for bad in bad_outputs:
                ok, msg = hypr_core.apply_monitor(bad, {"enabled": True})
                self.assertFalse(ok, f"should reject output: {bad!r}")
                self.assertTrue(msg)
            run_mock.assert_not_called()

    def test_hostile_monitor_fields_rejected(self):
        bad_cfgs = [
            {"enabled": "yes"},
            {"mode": "1920x1080@60\n"},
            {"mode": '1920x1080@60"\nos.execute("id")'},
            {"position": "0x0\n"},
            {"position": "0x0; os.execute('id')"},
            {"scale": float("nan")},
            {"scale": float("inf")},
            {"scale": True},
            {"scale": 0.01},
            {"transform": 9},
            {"transform": True},
            {"vrr": 99},
            {"vrr": False},
            {"mirror": "HDMI-A-1"},  # self-mirror
            {"mirror": "$(whoami)"},
            {"hdr": "true"},
            {"extra_injected": 1},
        ]
        with mock.patch.object(core, "run") as run_mock:
            for cfg in bad_cfgs:
                ok, _ = hypr_core.apply_monitor("HDMI-A-1", cfg)
                self.assertFalse(ok, f"should reject cfg: {cfg!r}")
            run_mock.assert_not_called()

    def test_hostile_keyboard_layouts_and_options_rejected(self):
        bad_layouts = [
            "us\n",
            "us\r",
            'us"\nos.execute("id")',
            "us\r\nde",
            "$(id)",
            "us;de",
            "us de",
            "us\\de",
            "a" * 129,
        ]
        with mock.patch.object(core, "run") as run_mock:
            for bad in bad_layouts:
                self.assertFalse(hypr_core.save_input({"kb_layout": bad})[0], repr(bad))
                self.assertFalse(hypr_core.save_input({"kb_layout": "us", "kb_layout2": bad})[0], repr(bad))
            self.assertFalse(hypr_core.save_input({"caps_option": "caps:escape\n--"})[0])
            self.assertFalse(hypr_core.save_input({"compose_option": "$(id)"})[0])
            self.assertFalse(hypr_core.save_input({"accel_profile": "fast"})[0])
            self.assertFalse(hypr_core.save_input({"repeat_delay": True})[0])
            self.assertFalse(hypr_core.save_input({"sensitivity": float("nan")})[0])
            run_mock.assert_not_called()

    def test_hostile_animation_styles_rejected(self):
        bad_styles = [
            "",
            'slide"\nos.execute("id")',
            "popin 50%\r\nfoo",
            "$(whoami)",
            "bounce",
            "slide;fade",
        ]
        with mock.patch.object(core, "run") as run_mock:
            for bad in bad_styles:
                self.assertFalse(hypr_core.save_animations({"windows_style": bad})[0], repr(bad))
                self.assertFalse(hypr_core.save_animations({"workspaces_style": bad})[0], repr(bad))
            self.assertFalse(hypr_core.save_animations({"windows_popin": 0})[0])
            self.assertFalse(hypr_core.save_animations({"windows_popin": 101})[0])
            self.assertFalse(hypr_core.save_animations({"windows_speed": float("inf")})[0])
            run_mock.assert_not_called()

    def test_render_lua_ignores_injected_raw_state(self):
        poisoned = {
            "monitors": {
                'DP-1"\nos.execute("id")': {"enabled": True},
                "DP-2": {"enabled": True, "mode": 'bad"\nfoo', "position": "$(id)", "mirror": 'x"\n'},
            },
            "input": {
                "kb_layout": 'us"\nos.execute("id")',
                "caps_option": 'caps:escape"\n',
                "accel_profile": 'flat"\n',
            },
            "animations": {
                "enabled": True,
                "windows_style": 'popin"\nos.execute("id")',
                "workspaces_style": "$(id)",
            },
        }
        lua = hypr_core.render_lua(poisoned)
        self.assertNotIn("os.execute", lua)
        self.assertNotIn("$(id)", lua)
        self.assertNotIn("DP-1", lua)

    def test_refuses_symlinked_settings_manager_lua(self):
        victim = self.root / "victim.txt"
        victim.write_text("do-not-touch")
        (self.hypr_dir / hypr_core.SETTINGS_LUA_NAME).symlink_to(victim)

        with mock.patch.object(core, "run") as run_mock:
            ok, msg = hypr_core.save_input({"kb_layout": "us"})
            self.assertFalse(ok)
            self.assertIn("symlink", msg.lower())
            run_mock.assert_not_called()
        self.assertEqual(victim.read_text(), "do-not-touch")

    def test_refuses_symlinked_hyprland_lua(self):
        victim = self.root / "victim_hyprland.lua"
        victim.write_text("original-victim\n")
        (self.hypr_dir / "hyprland.lua").symlink_to(victim)

        with mock.patch.object(core, "run") as run_mock:
            ok, msg = hypr_core.save_input({"kb_layout": "us"})
            self.assertFalse(ok)
            self.assertIn("symlink", msg.lower())
            ok_rm, msg_rm = hypr_core.remove_hook()
            self.assertFalse(ok_rm)
            self.assertIn("symlink", msg_rm.lower())
            run_mock.assert_not_called()
        self.assertEqual(victim.read_text(), "original-victim\n")
        self.assertFalse((self.hypr_dir / hypr_core.SETTINGS_LUA_NAME).exists())

    def test_refuses_symlinked_hypr_dir_and_world_writable_dir(self):
        real_dir = self.root / "real_hypr"
        real_dir.mkdir(mode=0o755)
        link_dir = self.root / "link_hypr"
        link_dir.symlink_to(real_dir)

        with mock.patch.object(hypr_core, "HYPR_DIR", link_dir), mock.patch.object(core, "run") as run_mock:
            ok, msg = hypr_core.save_input({"kb_layout": "us"})
            self.assertFalse(ok)
            self.assertIn("symlink", msg.lower())
            run_mock.assert_not_called()
        self.assertEqual(list(real_dir.iterdir()), [])

        unsafe_dir = self.root / "unsafe_hypr"
        unsafe_dir.mkdir()
        os.chmod(unsafe_dir, 0o777)
        with mock.patch.object(hypr_core, "HYPR_DIR", unsafe_dir), mock.patch.object(core, "run") as run_mock:
            ok, _ = hypr_core.save_input({"kb_layout": "us"})
            self.assertFalse(ok)
            run_mock.assert_not_called()

    def test_fifo_hyprland_lua_is_refused_without_blocking(self):
        fifo = self.hypr_dir / "hyprland.lua"
        os.mkfifo(fifo)
        with mock.patch.object(core, "run") as run_mock:
            ok, _ = hypr_core.save_input({"kb_layout": "us"})
            self.assertFalse(ok)
            run_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
