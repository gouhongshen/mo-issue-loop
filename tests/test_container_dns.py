from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

from scripts.container_dns import allow_container_dns, nameservers, resolver_config


class ContainerDnsTest(unittest.TestCase):
    def test_private_host_resolver_has_only_udp_tcp_dns_exceptions(self):
        run = Mock()
        allow_container_dns('nameserver 10.0.0.2\nnameserver 10.0.0.2 # duplicate\n', run)
        self.assertEqual(run.call_count, 2)
        for call, protocol in zip(run.call_args_list, ('udp', 'tcp')):
            self.assertEqual(call.args[0], ['sudo', 'iptables', '-A', 'MO_CONTAINER_OUT',
                '-d', '10.0.0.2/32', '-p', protocol, '--dport', '53', '-j', 'RETURN'])
            self.assertEqual(call.kwargs, {'check': True})

    def test_invalid_configuration_fails_before_installing_any_rule(self):
        for text in ('nameserver 10.0.0.2\nnameserver bad\n', 'nameserver 10.0.0.2 extra',
                     'nameserver 127.0.0.53', 'nameserver ::1', 'nameserver 0.0.0.0', ''):
            with self.subTest(text=text):
                run = Mock()
                with self.assertRaises(ValueError):
                    allow_container_dns(text, run)
                run.assert_not_called()

    def test_public_resolvers_and_options_are_preserved(self):
        self.assertEqual(nameservers('search local\nnameserver 1.1.1.1\noptions edns0\n'
                                     'nameserver 8.8.8.8\nnameserver 2001:4860:4860::8888'),
                         ['1.1.1.1', '8.8.8.8'])

    def test_systemd_stub_uses_the_same_upstream_file_as_docker(self):
        with tempfile.TemporaryDirectory() as directory:
            host, resolved = Path(directory) / 'host', Path(directory) / 'resolved'
            host.write_text('nameserver 127.0.0.53\noptions edns0 trust-ad\n')
            resolved.write_text('nameserver 10.0.0.2\n')
            self.assertEqual(nameservers(resolver_config(host, resolved)), ['10.0.0.2'])
            host.write_text('nameserver 8.8.8.8\n')
            self.assertEqual(nameservers(resolver_config(host, resolved)), ['8.8.8.8'])
            host.write_text('nameserver 127.0.0.53\n')
            resolved.unlink()
            with self.assertRaises(FileNotFoundError):
                resolver_config(host, resolved)

    def test_firewall_installs_dns_before_private_rejection_and_bridge_hook(self):
        # Execute the production shell, replacing privileged commands only.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, body in (('sudo', 'echo "RULE $*"'), ('python3',
                    'case "$1" in scripts/container_dns.py) echo DNS_EXCEPTIONS;; *) exit 0;; esac')):
                executable = root / name
                executable.write_text('#!/bin/sh\n' + body + '\n')
                executable.chmod(0o755)
            result = subprocess.check_output(['/bin/bash', 'scripts/firewall.sh'], text=True,
                env={'PATH': str(root) + ':/usr/bin:/bin', 'CONTROLLER_IP': '100.64.0.1'})
            lines = result.splitlines()
            dns = lines.index('DNS_EXCEPTIONS')
            create = lines.index('RULE iptables -N MO_CONTAINER_OUT')
            reject = lines.index('RULE iptables -A MO_CONTAINER_OUT -d 10.0.0.0/8 -j REJECT')
            hook = lines.index('RULE iptables -I DOCKER-USER 1 -i docker0 -j MO_CONTAINER_OUT')
            self.assertLess(create, dns)
            self.assertLess(dns, reject)
            self.assertLess(reject, hook)
            for network in ('100.64.0.0/10', '169.254.0.0/16', '172.16.0.0/12', '192.168.0.0/16'):
                self.assertIn('RULE iptables -A MO_CONTAINER_OUT -d ' + network + ' -j REJECT', lines)
