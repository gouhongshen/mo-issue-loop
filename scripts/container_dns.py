"""Permit the host's IPv4 resolvers through the container egress fence."""
import ipaddress
from pathlib import Path
import subprocess


def nameservers(text):
    addresses = []
    for line in text.splitlines():
        fields = line.split('#', 1)[0].split()
        if not fields or fields[0] != 'nameserver':
            continue
        if len(fields) != 2:
            raise ValueError('invalid host nameserver entry')
        address = ipaddress.ip_address(fields[1])
        # Docker's default IPv4 bridge needs a reachable upstream, not a
        # host-loopback stub or an IPv6-only resolver.
        if address.version == 4 and not (address.is_loopback or address.is_unspecified or address.is_multicast):
            if str(address) not in addresses:
                addresses.append(str(address))
    if not addresses:
        raise ValueError('no usable host IPv4 DNS resolver for the container bridge')
    return addresses


def allow_container_dns(text, run=subprocess.run):
    # Parse the complete trusted host configuration before installing rules.
    for address in nameservers(text):
        for protocol in ('udp', 'tcp'):
            run(['sudo', 'iptables', '-A', 'MO_CONTAINER_OUT', '-d', address + '/32',
                 '-p', protocol, '--dport', '53', '-j', 'RETURN'], check=True)


def resolver_config(host=Path('/etc/resolv.conf'), resolved=Path('/run/systemd/resolve/resolv.conf')):
    text = host.read_text()
    entries = [line.split('#', 1)[0].split() for line in text.splitlines()]
    entries = [fields[1:] for fields in entries if fields and fields[0] == 'nameserver']
    # Docker substitutes systemd-resolved's upstream file when the host uses
    # its single loopback stub. That stub is unreachable from the bridge.
    if entries == [['127.0.0.53']]:
        return resolved.read_text()
    return text


if __name__ == '__main__':
    config = resolver_config()
    allow_container_dns(config)
    print('Container DNS egress allowed (UDP/TCP 53 only): ' + ', '.join(nameservers(config)), flush=True)
