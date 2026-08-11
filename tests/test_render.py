import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from camarectl.config import load_config
from camarectl.render import RenderError, render_runtime


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if ".git" in relative.parts or "__pycache__" in relative.parts:
            continue
        if path.suffix in {".pyc", ".pyo"}:
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RenderTests(unittest.TestCase):
    def write_lan_config(self, root: Path, *, hardware: str = "cpu") -> Path:
        secrets_dir = root / "input-secrets"
        secrets_dir.mkdir(parents=True)
        (secrets_dir / "yard-password").write_text(
            "p@ss:word/%", encoding="utf-8"
        )
        if hardware == "cpu":
            hardware_values = "mode = cpu\nrender_device =\nva_driver ="
        else:
            hardware_values = (
                "mode = intel-vaapi\n"
                "render_device = /dev/dri/renderD128\n"
                "va_driver = i965"
            )
        config_path = root / "camare.ini"
        config_path.write_text(
            f"""[camare]
instance_id = render-test
runtime_dir = /opt/1panel/docker/compose/camare-render-test
timezone = Asia/Shanghai
provider = lan
groups = yard

[public]
origin =
loopback_port = 19080
cloudflare_token_file =

[admin]
origin =
loopback_port = 19081
tailscale_login =
frigate_bind_ip = 192.168.1.250
frigate_port = 18971

[lan]
bind_ip = 192.168.1.250
port = 18088

[storage]
media = /srv/camare-test/media
retention = 14

[hardware]
{hardware_values}

[group:yard]
name = 院子
host = 192.168.1.128
rtsp_user = admin
rtsp_port = 554
password_file = input-secrets/yard-password
record_audio = false
enabled = true
streams = yard_ptz, yard_fixed

[stream:yard:yard_ptz]
name = 云台画面
path = /live/101

[stream:yard:yard_fixed]
name = 枪机画面
path = /live/201
""",
            encoding="utf-8",
        )
        return config_path

    def write_cloudflare_two_group_config(self, root: Path) -> Path:
        config_path = self.write_lan_config(root, hardware="intel-vaapi")
        secret_dir = root / "input-secrets"
        (secret_dir / "front-password").write_text(
            "front?#/pass", encoding="utf-8"
        )
        (secret_dir / "cloudflare-token").write_bytes(b"cf-token-raw\n")
        content = config_path.read_text(encoding="utf-8")
        content = (
            content.replace("provider = lan", "provider = cloudflare")
            .replace("groups = yard", "groups = yard, front")
            .replace(
                "origin =\nloopback_port = 19080",
                "origin = https://camera.example.com\nloopback_port = 19080",
                1,
            )
            .replace(
                "cloudflare_token_file =",
                "cloudflare_token_file = input-secrets/cloudflare-token",
            )
            .replace(
                "origin =\nloopback_port = 19081\ntailscale_login =",
                "origin = https://camera-node.example.ts.net:9443\n"
                "loopback_port = 19081\n"
                "tailscale_login = owner@example.com",
            )
        )
        content += """

[group:front]
name = 门前
host = 192.168.1.129
rtsp_user = operator@example
rtsp_port = 554
password_file = input-secrets/front-password
record_audio = true
enabled = true
streams = front_ptz, front_fixed

[stream:front:front_ptz]
name = 门前云台
path = /live/101

[stream:front:front_fixed]
name = 门前固定
path = /live/201
"""
        config_path.write_text(content, encoding="utf-8")
        return config_path

    def test_cpu_lan_render_is_isolated_startable_and_secret_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_config(self.write_lan_config(root))
            source_before = tree_digest(REPOSITORY_ROOT)

            layout = render_runtime(
                config,
                source_root=REPOSITORY_ROOT,
                staging_parent=root / "staging",
                lan_access_key="a" * 64,
            )

            self.assertEqual(tree_digest(REPOSITORY_ROOT), source_before)
            self.assertEqual(layout.root.parent, root / "staging")
            self.assertTrue(layout.root.name.startswith("render-test.staging-"))
            self.assertIsNone(layout.intel_compose)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(layout.env.stat().st_mode), 0o600)
                self.assertEqual(
                    stat.S_IMODE(layout.frigate_config.stat().st_mode), 0o640
                )
                self.assertEqual(stat.S_IMODE(layout.caddyfile.stat().st_mode), 0o644)

            compose = layout.base_compose.read_text(encoding="utf-8")
            frigate = layout.frigate_config.read_text(encoding="utf-8")
            environment = layout.env.read_text(encoding="utf-8")
            manifest_text = layout.manifest.read_text(encoding="utf-8")
            self.assertIn("\n  frigate:\n", compose)
            self.assertIn("\n  camera-portal:\n", compose)
            self.assertIn("\n  lan-caddy:\n", compose)
            self.assertNotIn("\n  cloudflared:\n", compose)
            self.assertNotIn("container_name:", compose)
            self.assertNotIn("external:", compose)
            self.assertNotIn("devices:", compose)
            self.assertNotIn("LIBVA_DRIVER_NAME", compose)
            self.assertNotIn("preset-vaapi", frigate)
            self.assertIn('PUBLIC_BASE_URL="http://192.168.1.250:18088"', environment)
            self.assertIn('LOCAL_BASE_URL="http://192.168.1.250:18088"', environment)
            self.assertIn('ADMIN_DISABLED="true"', environment)
            self.assertIn('ADMIN_ORIGIN=""', environment)
            self.assertIn('ADMIN_TAILSCALE_LOGIN=""', environment)
            self.assertNotIn("LAN_ACCESS_KEY", environment)
            self.assertIn("LOCAL_ACCESS_KEY_FILE: /run/secrets/LAN_ACCESS_KEY", compose)
            self.assertIn("cat /run/secrets/LAN_ACCESS_KEY", compose)

            secret_path = layout.secrets / "camera-password-1"
            self.assertEqual(secret_path.read_text(encoding="utf-8"), "p%40ss%3Aword%2F%25")
            self.assertEqual(
                (layout.secrets / "lan-access-key").read_text(encoding="utf-8"),
                "a" * 64,
            )
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(secret_path.stat().st_mode), 0o600)
            for rendered in (
                compose,
                frigate,
                environment,
                manifest_text,
                layout.caddyfile.read_text(encoding="utf-8"),
            ):
                self.assertNotIn("p@ss:word/%", rendered)
                self.assertNotIn("a" * 64, rendered)

            manifest = json.loads(manifest_text)
            self.assertEqual(manifest["instance_id"], "render-test")
            self.assertEqual(manifest["provider"], "lan")
            self.assertEqual(manifest["build_mode"], "local")
            self.assertEqual(
                manifest["required_ownership"]["portal-data"],
                {"uid": 65532, "gid": 65532, "mode": "0750"},
            )
            self.assertIn(".env", manifest["files"])
            for relative, expected_digest in manifest["files"].items():
                self.assertEqual(file_digest(layout.root / relative), expected_digest)

    def test_intel_render_uses_only_the_configured_vaapi_device_and_driver(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_config(self.write_lan_config(root, hardware="intel-vaapi"))

            layout = render_runtime(
                config,
                source_root=REPOSITORY_ROOT,
                staging_parent=root / "staging",
                lan_access_key="b" * 64,
            )

            self.assertIsNotNone(layout.intel_compose)
            base = layout.base_compose.read_text(encoding="utf-8")
            override = layout.intel_compose.read_text(encoding="utf-8")
            environment = layout.env.read_text(encoding="utf-8")
            frigate = layout.frigate_config.read_text(encoding="utf-8")
            self.assertNotIn("devices:", base)
            self.assertNotIn("LIBVA_DRIVER_NAME", base)
            self.assertIn("devices:", override)
            self.assertIn("FRIGATE_RENDER_DEVICE", override)
            self.assertIn("LIBVA_DRIVER_NAME", override)
            self.assertIn('FRIGATE_RENDER_DEVICE="/dev/dri/renderD128"', environment)
            self.assertIn('LIBVA_DRIVER_NAME="i965"', environment)
            self.assertIn("hwaccel_args: preset-vaapi", frigate)
            self.assertEqual(
                json.loads(layout.manifest.read_text(encoding="utf-8"))[
                    "compose_files"
                ],
                ["compose.yaml", "compose.intel.yaml"],
            )

    def test_cloudflare_two_groups_render_four_streams_audio_and_raw_token_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_config(self.write_cloudflare_two_group_config(root))

            layout = render_runtime(
                config,
                source_root=REPOSITORY_ROOT,
                staging_parent=root / "staging",
                lan_access_key="c" * 64,
            )

            compose = layout.base_compose.read_text(encoding="utf-8")
            frigate = layout.frigate_config.read_text(encoding="utf-8")
            environment = layout.env.read_text(encoding="utf-8")
            manifest = layout.manifest.read_text(encoding="utf-8")
            self.assertIn("\n  cloudflared:\n", compose)
            self.assertIn("target: TUNNEL_TOKEN", compose)
            self.assertEqual(
                (layout.secrets / "cloudflare-token").read_bytes(),
                b"cf-token-raw\n",
            )
            self.assertEqual(
                (layout.secrets / "camera-password-2").read_text(encoding="utf-8"),
                "front%3F%23%2Fpass",
            )
            self.assertIn(
                'CAMERAS="yard_ptz=云台画面,yard_fixed=枪机画面,'
                'front_ptz=门前云台,front_fixed=门前固定"',
                environment,
            )
            self.assertIn(
                'CAMERA_GROUPS="yard=院子:yard_ptz|yard_fixed;'
                'front=门前:front_ptz|front_fixed"',
                environment,
            )
            self.assertIn('ADMIN_DISABLED="false"', environment)
            self.assertIn('PUBLIC_BASE_URL="https://camera.example.com"', environment)
            self.assertIn(
                'ADMIN_ORIGIN="https://camera-node.example.ts.net:9443"',
                environment,
            )
            for camera_id in (
                "yard_ptz",
                "yard_fixed",
                "front_ptz",
                "front_fixed",
            ):
                self.assertIn(f"    {camera_id}:", frigate)
                self.assertIn(f"  {camera_id}:\n", frigate)
            self.assertEqual(frigate.count("record: preset-record-generic\n"), 2)
            self.assertEqual(
                frigate.count("record: preset-record-generic-audio-aac\n"), 2
            )
            self.assertIn("{FRIGATE_RTSP_PASSWORD_GROUP_1}", frigate)
            self.assertIn("{FRIGATE_RTSP_PASSWORD_GROUP_2}", frigate)
            self.assertIn("operator%40example", frigate)
            for rendered in (compose, frigate, environment, manifest):
                self.assertNotIn("p@ss:word/%", rendered)
                self.assertNotIn("front?#/pass", rendered)
                self.assertNotIn("cf-token-raw", rendered)
                self.assertNotIn("c" * 64, rendered)
            self.assertEqual(
                (layout.secrets / "lan-access-key").read_text(encoding="utf-8"),
                "c" * 64,
            )

    def test_each_render_uses_a_new_staging_tree_without_overwriting_prior_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_config(self.write_lan_config(root))

            first = render_runtime(
                config,
                source_root=REPOSITORY_ROOT,
                staging_parent=root / "staging",
                lan_access_key="d" * 64,
            )
            first_manifest = first.manifest.read_bytes()
            second = render_runtime(
                config,
                source_root=REPOSITORY_ROOT,
                staging_parent=root / "staging",
                lan_access_key="e" * 64,
            )

            self.assertNotEqual(first.root, second.root)
            self.assertEqual(first.manifest.read_bytes(), first_manifest)
            self.assertTrue(first.root.is_dir())
            self.assertTrue(second.root.is_dir())

    def test_render_rejects_staging_inside_the_source_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = load_config(self.write_lan_config(Path(temporary)))

            with self.assertRaisesRegex(RenderError, "outside the source checkout"):
                render_runtime(
                    config,
                    source_root=REPOSITORY_ROOT,
                    staging_parent=REPOSITORY_ROOT / "runtime",
                    lan_access_key="f" * 64,
                )

    def test_compose_dotenv_values_round_trip_unicode_spaces_quotes_and_backslashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self.write_lan_config(root)
            content = config_path.read_text(encoding="utf-8")
            content = content.replace("name = 院子", "name = 院子 O'Brien\\西侧")
            content = content.replace(
                "name = 云台画面", "name = 云台 O'Brien\\东侧"
            )
            config_path.write_text(content, encoding="utf-8")
            config = load_config(config_path)

            layout = render_runtime(
                config,
                source_root=REPOSITORY_ROOT,
                staging_parent=root / "staging",
                lan_access_key="g" * 64,
            )

            values = {}
            for line in layout.env.read_text(encoding="utf-8").splitlines():
                key, encoded = line.split("=", 1)
                values[key] = json.loads(encoded)
            self.assertEqual(
                values["CAMERAS"],
                "yard_ptz=云台 O'Brien\\东侧,yard_fixed=枪机画面",
            )
            self.assertEqual(
                values["CAMERA_GROUPS"],
                "yard=院子 O'Brien\\西侧:yard_ptz|yard_fixed",
            )

    def test_tailscale_uses_declared_origins_without_adding_cloudflared(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self.write_lan_config(root)
            content = (
                config_path.read_text(encoding="utf-8")
                .replace("provider = lan", "provider = tailscale")
                .replace(
                    "origin =\nloopback_port = 19080",
                    "origin = https://camera-node.example.ts.net:8443\n"
                    "loopback_port = 19080",
                    1,
                )
                .replace(
                    "origin =\nloopback_port = 19081\ntailscale_login =",
                    "origin = https://camera-node.example.ts.net:9443\n"
                    "loopback_port = 19081\n"
                    "tailscale_login = owner@example.com",
                )
            )
            config_path.write_text(content, encoding="utf-8")
            config = load_config(config_path)

            layout = render_runtime(
                config,
                source_root=REPOSITORY_ROOT,
                staging_parent=root / "staging",
                lan_access_key="h" * 64,
            )

            compose = layout.base_compose.read_text(encoding="utf-8")
            values = {
                key: json.loads(encoded)
                for key, encoded in (
                    line.split("=", 1)
                    for line in layout.env.read_text(encoding="utf-8").splitlines()
                )
            }
            self.assertNotIn("\n  cloudflared:\n", compose)
            self.assertEqual(values["ADMIN_DISABLED"], "false")
            self.assertEqual(
                values["PUBLIC_BASE_URL"],
                "https://camera-node.example.ts.net:8443",
            )
            self.assertEqual(
                values["ADMIN_ORIGIN"],
                "https://camera-node.example.ts.net:9443",
            )
            self.assertEqual(values["ADMIN_TAILSCALE_LOGIN"], "owner@example.com")

    def test_one_stream_group_reserves_the_second_portal_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self.write_lan_config(root)
            content = config_path.read_text(encoding="utf-8")
            content = content.replace(
                "streams = yard_ptz, yard_fixed", "streams = yard_ptz"
            )
            content = content.replace(
                "\n[stream:yard:yard_fixed]\n"
                "name = 枪机画面\n"
                "path = /live/201\n",
                "\n",
            )
            config_path.write_text(content, encoding="utf-8")
            config = load_config(config_path)

            layout = render_runtime(
                config,
                source_root=REPOSITORY_ROOT,
                staging_parent=root / "staging",
                lan_access_key="m" * 64,
            )

            values = {
                key: json.loads(encoded)
                for key, encoded in (
                    line.split("=", 1)
                    for line in layout.env.read_text(encoding="utf-8").splitlines()
                )
            }
            self.assertEqual(values["CAMERAS"], "yard_ptz=云台画面")
            self.assertEqual(values["CAMERA_GROUPS"], "yard=院子:yard_ptz|")
            self.assertNotIn(
                "yard_fixed",
                layout.frigate_config.read_text(encoding="utf-8"),
            )

    def test_disabled_groups_are_not_rendered_and_at_least_one_must_remain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self.write_cloudflare_two_group_config(root)
            content = config_path.read_text(encoding="utf-8").replace(
                "[group:front]\nname = 门前",
                "[group:front]\nname = 门前",
            )
            second_enabled = content.rfind("enabled = true")
            content = (
                content[:second_enabled]
                + "enabled = false"
                + content[second_enabled + len("enabled = true") :]
            )
            config_path.write_text(content, encoding="utf-8")
            config = load_config(config_path)

            layout = render_runtime(
                config,
                source_root=REPOSITORY_ROOT,
                staging_parent=root / "staging",
                lan_access_key="i" * 64,
            )

            rendered = "\n".join(
                (
                    layout.frigate_config.read_text(encoding="utf-8"),
                    layout.env.read_text(encoding="utf-8"),
                    layout.base_compose.read_text(encoding="utf-8"),
                )
            )
            self.assertNotIn("front_ptz", rendered)
            self.assertNotIn("front_fixed", rendered)
            self.assertFalse((layout.secrets / "camera-password-2").exists())

            no_enabled_path = self.write_lan_config(root / "all-disabled")
            no_enabled_content = no_enabled_path.read_text(encoding="utf-8").replace(
                "enabled = true", "enabled = false"
            )
            no_enabled_path.write_text(no_enabled_content, encoding="utf-8")
            no_enabled = load_config(no_enabled_path)
            with self.assertRaisesRegex(RenderError, "enabled groups"):
                render_runtime(
                    no_enabled,
                    source_root=REPOSITORY_ROOT,
                    staging_parent=root / "unused-staging",
                    lan_access_key="j" * 64,
                )

    def test_failed_secret_render_removes_its_partial_staging_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self.write_lan_config(root)
            (root / "input-secrets" / "yard-password").write_text(
                "invalid\npassword", encoding="utf-8"
            )
            config = load_config(config_path)
            staging = root / "staging"

            with self.assertRaisesRegex(RenderError, "without line breaks"):
                render_runtime(
                    config,
                    source_root=REPOSITORY_ROOT,
                    staging_parent=staging,
                    lan_access_key="k" * 64,
                )

            self.assertEqual(list(staging.iterdir()), [])

    def test_explicit_empty_lan_access_key_is_rejected_instead_of_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_config(self.write_lan_config(root))

            with self.assertRaisesRegex(RenderError, "lan_access_key"):
                render_runtime(
                    config,
                    source_root=REPOSITORY_ROOT,
                    staging_parent=root / "staging",
                    lan_access_key="",
                )

    def test_portal_delimiters_in_display_names_are_rejected(self) -> None:
        replacements = (
            ("name = 院子", "name = 院子=东"),
            ("name = 院子", "name = 院子|东"),
            ("name = 云台画面", "name = 云台=画面"),
        )
        for original, invalid in replacements:
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config_path = self.write_lan_config(root)
                content = config_path.read_text(encoding="utf-8").replace(
                    original, invalid, 1
                )
                config_path.write_text(content, encoding="utf-8")
                config = load_config(config_path)

                with self.assertRaisesRegex(RenderError, "Portal delimiter"):
                    render_runtime(
                        config,
                        source_root=REPOSITORY_ROOT,
                        staging_parent=root / "staging",
                        lan_access_key="l" * 64,
                    )


if __name__ == "__main__":
    unittest.main()
