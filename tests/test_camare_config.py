import os
import tempfile
import unittest
from pathlib import Path, PurePosixPath

from camarectl.config import ConfigError, load_config
from camarectl.model import (
    AdminConfig,
    DeploymentConfig,
    GroupConfig,
    HardwareConfig,
    LanConfig,
    PublicConfig,
    StreamConfig,
)


VALID_CONFIG = """[camare]
instance_id = home-camera
runtime_dir = /var/lib/camare
timezone = Asia/Shanghai
provider = lan
groups = yard

[public]
origin =
loopback_port = 9080
cloudflare_token_file =

[admin]
origin =
loopback_port = 9081
tailscale_login =
frigate_bind_ip = 192.168.1.250
frigate_port = 8971

[lan]
bind_ip = 192.168.1.250
port = 8088

[storage]
media = /srv/frigate/media
retention = 14

[hardware]
mode = cpu
render_device =
va_driver =

[group:yard]
name = Yard
host = 192.168.1.128
rtsp_user = admin
rtsp_port = 554
password_file = secrets/yard.password
record_audio = false
enabled = true
streams = ptz_view, fixed_view

[stream:yard:ptz_view]
name = PTZ view
path = /live/101

[stream:yard:fixed_view]
name = Fixed view
path = /live/201
"""


class CamareConfigTests(unittest.TestCase):
    def write_config(self, root: Path, content: str = VALID_CONFIG) -> Path:
        secret = root / "secrets" / "yard.password"
        secret.parent.mkdir()
        secret.write_text("not-a-real-password", encoding="utf-8")
        config_path = root / "camare.ini"
        config_path.write_text(content, encoding="utf-8")
        return config_path

    def test_loads_minimal_lan_deployment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self.write_config(root)
            secret = root / "secrets" / "yard.password"

            config = load_config(config_path)

        self.assertIsInstance(config, DeploymentConfig)
        self.assertEqual(config.instance_id, "home-camera")
        self.assertEqual(config.runtime_dir, PurePosixPath("/var/lib/camare"))
        self.assertEqual(config.timezone, "Asia/Shanghai")
        self.assertEqual(config.provider, "lan")
        self.assertEqual(
            config.public,
            PublicConfig(origin="", loopback_port=9080, cloudflare_token_file=None),
        )
        self.assertEqual(
            config.admin,
            AdminConfig(
                origin="",
                loopback_port=9081,
                tailscale_login="",
                frigate_bind_ip="192.168.1.250",
                frigate_port=8971,
            ),
        )
        self.assertEqual(config.lan, LanConfig(bind_ip="192.168.1.250", port=8088))
        self.assertEqual(config.media, PurePosixPath("/srv/frigate/media"))
        self.assertEqual(config.retention, 14.0)
        self.assertEqual(
            config.hardware,
            HardwareConfig(mode="cpu", render_device=None, va_driver=None),
        )
        self.assertEqual(len(config.groups), 1)

        group = config.groups[0]
        self.assertIsInstance(group, GroupConfig)
        self.assertEqual(group.id, "yard")
        self.assertEqual(group.name, "Yard")
        self.assertEqual(group.host, "192.168.1.128")
        self.assertEqual(group.rtsp_user, "admin")
        self.assertEqual(group.rtsp_port, 554)
        self.assertEqual(group.password_file, secret.resolve())
        self.assertFalse(group.record_audio)
        self.assertTrue(group.enabled)
        self.assertEqual(
            group.streams,
            (
                StreamConfig(id="ptz_view", name="PTZ view", path="/live/101"),
                StreamConfig(id="fixed_view", name="Fixed view", path="/live/201"),
            ),
        )

    def test_rejects_unknown_top_level_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = VALID_CONFIG.replace(
                "provider = lan", "provider = lan\nsecret_password = leaked"
            )
            config_path = self.write_config(root, content)

            with self.assertRaisesRegex(ConfigError, "unknown key.*secret_password"):
                load_config(config_path)

    def test_rejects_missing_password_file_without_reading_a_secret_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self.write_config(root)
            (root / "secrets" / "yard.password").unlink()

            with self.assertRaisesRegex(
                ConfigError, "password_file.*does not exist"
            ):
                load_config(config_path)

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits are enforced on Linux")
    def test_rejects_group_or_other_readable_secret_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self.write_config(root)
            secret = root / "secrets" / "yard.password"
            secret.chmod(0o644)

            with self.assertRaisesRegex(ConfigError, "chmod 600"):
                load_config(config_path)

            secret.chmod(0o600)
            load_config(config_path)

    def test_rejects_undeclared_section(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self.write_config(
                root,
                VALID_CONFIG + "\n[stream:yard:unexpected]\nname = Hidden\npath = /hidden\n",
            )

            with self.assertRaisesRegex(
                ConfigError, "unknown section.*stream:yard:unexpected"
            ):
                load_config(config_path)

    def test_rejects_unsafe_instance_group_and_stream_ids(self) -> None:
        replacements = {
            "home-camera": "../home",
            "groups = yard": "groups = Yard",
            "streams = ptz_view, fixed_view": "streams = ptz.view, fixed_view",
        }
        for original, invalid in replacements.items():
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config_path = self.write_config(
                    root, VALID_CONFIG.replace(original, invalid)
                )

                with self.assertRaisesRegex(ConfigError, "unsafe.*id"):
                    load_config(config_path)

    def test_requires_one_or_two_groups_and_one_or_two_streams_per_group(self) -> None:
        invalid_documents = (
            VALID_CONFIG.replace("groups = yard", "groups ="),
            VALID_CONFIG.replace("groups = yard", "groups = yard, yard, yard"),
            VALID_CONFIG.replace(
                "streams = ptz_view, fixed_view", "streams ="
            ),
            VALID_CONFIG.replace(
                "streams = ptz_view, fixed_view",
                "streams = ptz_view, fixed_view, third",
            ),
        )
        for content in invalid_documents:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temporary:
                config_path = self.write_config(Path(temporary), content)

                with self.assertRaisesRegex(ConfigError, "between 1 and 2"):
                    load_config(config_path)

    def test_rejects_duplicate_group_or_stream_ids(self) -> None:
        second_group = """
[group:front]
name = Front
host = 192.168.1.129
rtsp_user = admin
rtsp_port = 554
password_file = secrets/yard.password
record_audio = true
enabled = true
streams = ptz_view

[stream:front:ptz_view]
name = Duplicate camera id
path = /live/101
"""
        invalid_documents = (
            VALID_CONFIG.replace("groups = yard", "groups = yard, yard"),
            VALID_CONFIG.replace(
                "streams = ptz_view, fixed_view", "streams = ptz_view, ptz_view"
            ),
            VALID_CONFIG.replace("groups = yard", "groups = yard, front")
            + second_group,
        )
        for content in invalid_documents:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temporary:
                config_path = self.write_config(Path(temporary), content)

                with self.assertRaisesRegex(ConfigError, "duplicate.*id"):
                    load_config(config_path)

    def test_hardware_is_cpu_or_complete_intel_vaapi(self) -> None:
        invalid_hardware_sections = (
            "mode = nvidia\nrender_device =\nva_driver =",
            "mode = cpu\nrender_device = /dev/dri/renderD128\nva_driver = i965",
            "mode = intel-vaapi\nrender_device =\nva_driver =",
            "mode = intel-vaapi\nrender_device = /dev/dri/card0\nva_driver = i965",
        )
        original = "mode = cpu\nrender_device =\nva_driver ="
        for hardware in invalid_hardware_sections:
            with self.subTest(hardware=hardware), tempfile.TemporaryDirectory() as temporary:
                config_path = self.write_config(
                    Path(temporary), VALID_CONFIG.replace(original, hardware)
                )

                with self.assertRaisesRegex(ConfigError, "hardware"):
                    load_config(config_path)

        with tempfile.TemporaryDirectory() as temporary:
            config_path = self.write_config(
                Path(temporary),
                VALID_CONFIG.replace(
                    original,
                    "mode = intel-vaapi\n"
                    "render_device = /dev/dri/renderD128\n"
                    "va_driver = i965",
                ),
            )

            config = load_config(config_path)

        self.assertEqual(
            config.hardware,
            HardwareConfig(
                mode="intel-vaapi",
                render_device=PurePosixPath("/dev/dri/renderD128"),
                va_driver="i965",
            ),
        )

    def test_cloudflare_requires_token_reference_and_never_loads_secret_values(self) -> None:
        cloudflare = (
            VALID_CONFIG.replace("provider = lan", "provider = cloudflare")
            .replace("origin =\nloopback_port = 9080", "origin = https://camera.lol-lu.site\nloopback_port = 9080", 1)
            .replace(
                "cloudflare_token_file =",
                "cloudflare_token_file = secrets/cloudflare.token",
            )
            .replace(
                "origin =\nloopback_port = 9081\ntailscale_login =",
                "origin = https://camera-node.example-tailnet.ts.net:9443\n"
                "loopback_port = 9081\n"
                "tailscale_login = owner@example.com",
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self.write_config(root, cloudflare)

            with self.assertRaisesRegex(ConfigError, "cloudflare_token_file.*does not exist"):
                load_config(config_path)

            token_file = root / "secrets" / "cloudflare.token"
            token_file.write_text("CF-TOKEN-SUPERSECRET", encoding="utf-8")
            config = load_config(config_path)

        self.assertEqual(config.public.cloudflare_token_file, token_file.resolve())
        rendered = repr(config)
        self.assertNotIn("CF-TOKEN-SUPERSECRET", rendered)
        self.assertNotIn("not-a-real-password", rendered)
        self.assertNotIn("cloudflare_token_file", rendered)
        self.assertNotIn("password_file", rendered)

    def test_provider_origins_follow_lan_cloudflare_and_tailscale_boundaries(self) -> None:
        cloudflare = (
            VALID_CONFIG.replace("provider = lan", "provider = cloudflare")
            .replace("origin =\nloopback_port = 9080", "origin = https://camera.lol-lu.site\nloopback_port = 9080", 1)
            .replace(
                "cloudflare_token_file =",
                "cloudflare_token_file = secrets/cloudflare.token",
            )
            .replace(
                "origin =\nloopback_port = 9081\ntailscale_login =",
                "origin = https://camera-node.example-tailnet.ts.net:9443\n"
                "loopback_port = 9081\n"
                "tailscale_login = owner@example.com",
            )
        )
        invalid_documents = (
            VALID_CONFIG.replace(
                "origin =\nloopback_port = 9080",
                "origin = https://camera.lol-lu.site\nloopback_port = 9080",
                1,
            ),
            cloudflare.replace(
                "https://camera.lol-lu.site", "http://camera.lol-lu.site"
            ),
            cloudflare.replace(
                "https://camera-node.example-tailnet.ts.net:9443",
                "https://admin.lol-lu.site:9443",
            ),
            cloudflare.replace("owner@example.com", ""),
            cloudflare.replace("provider = cloudflare", "provider = tailscale").replace(
                "https://camera.lol-lu.site",
                "https://public-not-tailscale.lol-lu.site:8443",
            ),
        )
        for content in invalid_documents:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config_path = self.write_config(root, content)
                token = root / "secrets" / "cloudflare.token"
                token.write_text("not-a-real-token", encoding="utf-8")

                with self.assertRaises(ConfigError):
                    load_config(config_path)

    def test_rejects_unsafe_runtime_media_timezone_and_retention(self) -> None:
        replacements = (
            ("runtime_dir = /var/lib/camare", "runtime_dir = runtime"),
            ("media = /srv/frigate/media", "media = /etc"),
            ("timezone = Asia/Shanghai", "timezone = ../etc/passwd"),
            ("retention = 14", "retention = 0"),
            ("retention = 14", "retention = 366"),
        )
        for original, invalid in replacements:
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temporary:
                config_path = self.write_config(
                    Path(temporary), VALID_CONFIG.replace(original, invalid)
                )

                with self.assertRaises(ConfigError):
                    load_config(config_path)

    def test_timezone_syntax_does_not_depend_on_the_local_zoneinfo_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_path = self.write_config(
                Path(temporary),
                VALID_CONFIG.replace("Asia/Shanghai", "Example/Deployment_Zone"),
            )

            config = load_config(config_path)

        self.assertEqual(config.timezone, "Example/Deployment_Zone")

    def test_rejects_unsafe_bind_addresses_and_ports(self) -> None:
        replacements = (
            (
                "[lan]\nbind_ip = 192.168.1.250",
                "[lan]\nbind_ip = 0.0.0.0",
            ),
            (
                "[lan]\nbind_ip = 192.168.1.250",
                "[lan]\nbind_ip = 100.64.0.10",
            ),
            ("frigate_bind_ip = 192.168.1.250", "frigate_bind_ip = 8.8.8.8"),
            ("loopback_port = 9080", "loopback_port = 0"),
            ("loopback_port = 9081", "loopback_port = 65536"),
            ("port = 8088", "port = -1"),
            ("frigate_port = 8971", "frigate_port = 70000"),
        )
        for original, invalid in replacements:
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temporary:
                config_path = self.write_config(
                    Path(temporary), VALID_CONFIG.replace(original, invalid, 1)
                )

                with self.assertRaises(ConfigError):
                    load_config(config_path)

    def test_rejects_invalid_group_and_stream_values(self) -> None:
        replacements = (
            ("name = Yard", "name ="),
            ("host = 192.168.1.128", "host = rtsp://192.168.1.128"),
            ("rtsp_user = admin", "rtsp_user ="),
            ("rtsp_port = 554", "rtsp_port = 0"),
            ("record_audio = false", "record_audio = yes"),
            ("enabled = true", "enabled = maybe"),
            ("name = PTZ view", "name ="),
            ("path = /live/101", "path = live/101"),
            ("path = /live/101", "path = /live/{camera}"),
        )
        for original, invalid in replacements:
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temporary:
                config_path = self.write_config(
                    Path(temporary), VALID_CONFIG.replace(original, invalid, 1)
                )

                with self.assertRaises(ConfigError):
                    load_config(config_path)

    def test_example_ini_matches_the_public_configuration_contract(self) -> None:
        example_path = Path(__file__).resolve().parents[1] / "camare.example.ini"
        example = example_path.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secret = root / "secrets" / "yard-rtsp-password"
            secret.parent.mkdir()
            secret.write_text("not-a-real-password", encoding="utf-8")
            config_path = root / "camare.ini"
            config_path.write_text(example, encoding="utf-8")

            config = load_config(config_path)

        self.assertEqual(config.instance_id, "home-camera")
        self.assertEqual(config.provider, "lan")
        self.assertEqual(config.hardware.mode, "intel-vaapi")
        self.assertEqual(tuple(group.id for group in config.groups), ("yard",))
        self.assertEqual(
            tuple(stream.id for stream in config.groups[0].streams),
            ("yard_ptz", "yard_fixed"),
        )


if __name__ == "__main__":
    unittest.main()
