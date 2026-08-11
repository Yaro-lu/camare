import os
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

import deploy


class ValidationTests(unittest.TestCase):
    def test_lan_ip_only_accepts_rfc1918(self) -> None:
        self.assertEqual(deploy.validate_lan_ip("192.168.1.250"), "192.168.1.250")
        self.assertEqual(deploy.validate_lan_ip("10.2.3.4"), "10.2.3.4")
        for invalid in ("100.64.0.10", "127.0.0.1", "8.8.8.8", "::1"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                deploy.validate_lan_ip(invalid)

    def test_admin_bind_accepts_lan_tailscale_and_loopback(self) -> None:
        for valid in ("192.168.1.250", "100.64.0.10", "127.0.0.1"):
            with self.subTest(valid=valid):
                self.assertEqual(deploy.validate_bind_ip(valid), valid)
        with self.assertRaises(ValueError):
            deploy.validate_bind_ip("8.8.8.8")

    def test_linux_path_validation_is_platform_independent(self) -> None:
        self.assertEqual(
            deploy.validate_linux_absolute_path("/srv/frigate/media"),
            "/srv/frigate/media",
        )
        for invalid in ("srv/frigate", "/srv/../etc", "C:\\recordings"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                deploy.validate_linux_absolute_path(invalid)

    def test_media_and_render_paths_are_narrowed(self) -> None:
        self.assertEqual(
            deploy.validate_media_path("/mnt/nas/camera"), "/mnt/nas/camera"
        )
        self.assertEqual(
            deploy.validate_render_device("/dev/dri/renderD128"),
            "/dev/dri/renderD128",
        )
        for invalid in ("/", "/etc", "/mnt", "/dev/dri/renderD128"):
            with self.subTest(media=invalid), self.assertRaises(ValueError):
                deploy.validate_media_path(invalid)
        for invalid in ("/dev/dri/card0", "/etc/passwd"):
            with self.subTest(render=invalid), self.assertRaises(ValueError):
                deploy.validate_render_device(invalid)

    def test_https_origin_rejects_path_and_credentials(self) -> None:
        self.assertEqual(
            deploy.validate_https_origin("https://camera.example.com/"),
            "https://camera.example.com",
        )
        for invalid in (
            "http://camera.example.com",
            "https://camera.example.com/path",
            "https://user:pass@camera.example.com",
            "https://@camera.example.com",
            "https://bad host.example.com",
            "https://camera.example.com:99999",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                deploy.validate_https_origin(invalid)

    def test_real_and_tailscale_origins_reject_placeholders(self) -> None:
        self.assertEqual(
            deploy.validate_deploy_origin("https://camera.user-domain.org"),
            "https://camera.user-domain.org",
        )
        self.assertEqual(
            deploy.validate_tailscale_origin(
                "https://camera-server.tailnet-name.ts.net:9443"
            ),
            "https://camera-server.tailnet-name.ts.net:9443",
        )
        for invalid in (
            "https://camera.example.com",
            "https://camera.example.org",
            "https://camera.invalid",
            "https://192.0.2.10",
            "https://camera.user-domain.org",
        ):
            validator = (
                deploy.validate_deploy_origin
                if invalid != "https://camera.user-domain.org"
                else deploy.validate_tailscale_origin
            )
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validator(invalid)


class ConfigurationTests(unittest.TestCase):
    def test_cloudflared_image_is_pinned_and_legacy_latest_is_migrated(self) -> None:
        pinned = deploy.CLOUDFLARED_IMAGE
        self.assertIn("@sha256:", pinned)
        self.assertEqual(deploy.normalize_cloudflared_image(""), pinned)
        self.assertEqual(
            deploy.normalize_cloudflared_image("cloudflare/cloudflared:latest"),
            pinned,
        )
        self.assertEqual(deploy.normalize_cloudflared_image(pinned), pinned)
        with self.assertRaisesRegex(deploy.InstallerError, "digest"):
            deploy.normalize_cloudflared_image("cloudflare/cloudflared:2026.7.3")

    def test_env_round_trip_and_newline_rejection(self) -> None:
        mapping = {
            "SIMPLE": "value",
            "CHINESE": "云台画面",
            "SPACES": "hello world",
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env"
            path.write_text(deploy.render_env(mapping), encoding="utf-8")
            self.assertEqual(deploy.parse_env_file(path), mapping)
        with self.assertRaises(deploy.InstallerError):
            deploy.encode_env_value("bad\nvalue")

    def test_lan_key_is_reused_and_mismatch_fails_closed(self) -> None:
        first = "a" * 64
        second = "b" * 64
        self.assertEqual(
            deploy.choose_lan_key({"LAN_ACCESS_KEY": first}, {}), first
        )
        generated = deploy.choose_lan_key({}, {})
        self.assertRegex(generated, r"^[0-9a-f]{64}$")
        with self.assertRaises(deploy.InstallerError):
            deploy.choose_lan_key(
                {"LAN_ACCESS_KEY": first}, {"LAN_ACCESS_KEY": second}
            )

    def test_retention_update_is_scoped_to_continuous_days(self) -> None:
        source = """record:
  enabled: true
  continuous:
    days: 0.125
  motion:
    days: 0
"""
        updated = deploy.update_retention_days(source, 7)
        self.assertIn("continuous:\n    days: 7", updated)
        self.assertIn("motion:\n    days: 0", updated)

    def test_runtime_files_keep_secrets_out_of_env(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = {
                "PORTAL_ENV": root / "camera-portal" / ".env",
                "LAN_ENV": root / "camera-portal-lan" / ".env",
                "FRIGATE_ENV": root / "frigate-1panel" / ".env",
                "CLOUDFLARE_ENV": root / "cloudflare" / ".env",
                "CAMERA_PASSWORD_FILE": root
                / "frigate-1panel"
                / "secrets"
                / "FRIGATE_RTSP_PASSWORD",
                "CLOUDFLARE_TOKEN_FILE": root
                / "cloudflare"
                / "secrets"
                / "TUNNEL_TOKEN",
                "FRIGATE_CONFIG": root
                / "frigate-1panel"
                / "config"
                / "config.yml",
            }
            paths["FRIGATE_CONFIG"].parent.mkdir(parents=True)
            paths["FRIGATE_CONFIG"].write_text(
                "record:\n  continuous:\n    days: 0.125\n", encoding="utf-8"
            )
            paths["FRIGATE_ENV"].parent.mkdir(parents=True, exist_ok=True)
            paths["FRIGATE_ENV"].write_text(
                "CUSTOM_SETTING=keep-me\n"
                "FRIGATE_RTSP_PASSWORD=must-not-stay-in-env\n",
                encoding="utf-8",
            )
            config = deploy.InstallConfig(
                lan_ip="192.168.1.250",
                lan_port=8088,
                camera_host="192.168.1.128",
                rtsp_user="admin",
                rtsp_port=554,
                ptz_path="/live/101",
                fixed_path="/live/201",
                ptz_name="云台画面",
                fixed_name="枪机画面",
                media_path=PurePosixPath("/srv/frigate/media"),
                create_media_path=False,
                retention_days=7,
                render_device=PurePosixPath("/dev/dri/renderD128"),
                va_driver="i965",
                frigate_admin_ip="100.64.0.10",
                frigate_admin_port=8971,
                provider="cloudflare",
                public_base_url="https://camera.example.com",
                admin_origin="https://camera-server.example.ts.net:9443",
                admin_login="owner@example.com",
                configure_tailscale=False,
            )
            secret_inputs = deploy.SecretInputs(
                camera_password="p@ss:word", cloudflare_token="secret-token"
            )
            with mock.patch.multiple(deploy, **paths):
                deploy.write_runtime_files(config, secret_inputs, "a" * 64)

            all_env = "\n".join(
                paths[name].read_text(encoding="utf-8")
                for name in (
                    "PORTAL_ENV",
                    "LAN_ENV",
                    "FRIGATE_ENV",
                    "CLOUDFLARE_ENV",
                )
            )
            self.assertNotIn("p@ss:word", all_env)
            self.assertNotIn("secret-token", all_env)
            self.assertNotIn("must-not-stay-in-env", all_env)
            self.assertIn("CUSTOM_SETTING=keep-me", all_env)
            self.assertEqual(
                deploy.parse_env_file(paths["PORTAL_ENV"])["CAMERA_GROUPS"],
                "yard=院子:ptz_view|fixed_view;front=门前:|",
            )
            self.assertEqual(
                paths["CAMERA_PASSWORD_FILE"].read_text(encoding="utf-8"),
                "p%40ss%3Aword",
            )
            self.assertEqual(
                paths["CLOUDFLARE_TOKEN_FILE"].read_text(encoding="utf-8"),
                "secret-token",
            )
            self.assertIn(
                "days: 7",
                paths["FRIGATE_CONFIG"].read_text(encoding="utf-8"),
            )
            if os.name != "nt":
                self.assertEqual(
                    paths["CAMERA_PASSWORD_FILE"].stat().st_mode & 0o777, 0o600
                )


if __name__ == "__main__":
    unittest.main()
