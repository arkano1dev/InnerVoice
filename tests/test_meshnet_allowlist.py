import importlib.machinery
import importlib.util
from pathlib import Path
import socket
import unittest
from unittest.mock import patch


path = Path(__file__).resolve().parents[1] / "bin/innervoice"
loader = importlib.machinery.SourceFileLoader("gateway_cli", str(path))
spec = importlib.util.spec_from_loader(loader.name, loader)
gateway_cli = importlib.util.module_from_spec(spec)
loader.exec_module(gateway_cli)


class MeshnetAllowlistTests(unittest.TestCase):
    def test_validate_saved_names_without_resolving(self):
        with patch.object(socket, "getaddrinfo") as lookup:
            self.assertEqual(
                gateway_cli.client_networks("Desktop.User-Example.NORD., 192.168.0.5"),
                "desktop.user-example.nord,192.168.0.5/32",
            )
        lookup.assert_not_called()

    def test_resolve_to_one_deduplicated_meshnet_ipv4(self):
        record = (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("100.64.0.5", 0))
        with patch.object(socket, "getaddrinfo", return_value=[record, record]) as lookup:
            self.assertEqual(
                gateway_cli.client_networks("desktop-example.nord,100.64.0.5", resolve=True),
                "100.64.0.5/32",
            )
        lookup.assert_called_once_with("desktop-example.nord", None, family=socket.AF_INET, type=socket.SOCK_STREAM)

    def test_reject_ambiguous_or_non_meshnet_resolution(self):
        for addresses in (["192.168.0.5"], ["100.64.0.5", "100.64.0.6"], []):
            records = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0)) for ip in addresses]
            with self.subTest(addresses=addresses), patch.object(socket, "getaddrinfo", return_value=records):
                with self.assertRaisesRegex(ValueError, "one Meshnet IPv4"):
                    gateway_cli.client_networks("desktop-example.nord", resolve=True)

    def test_fail_closed_on_resolution_error_even_with_numeric_peer(self):
        with patch.object(socket, "getaddrinfo", side_effect=socket.gaierror):
            with self.assertRaisesRegex(ValueError, "could not be resolved"):
                gateway_cli.client_networks("192.168.0.5,desktop-example.nord", resolve=True)


if __name__ == "__main__":
    unittest.main()
