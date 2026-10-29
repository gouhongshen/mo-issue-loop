#!/usr/bin/env bash
set -euo pipefail
# Only the disposable GitHub VM is touched. Install before tailscaled starts.
: "${CONTROLLER_IP:?missing controller Tailscale IPv4}"
python3 -c 'import ipaddress,os; a=ipaddress.ip_address(os.environ["CONTROLLER_IP"]); assert a in ipaddress.ip_network("100.64.0.0/10")'
for tool in iptables ip6tables; do
  sudo "$tool" -N MO_TS_IN
  sudo "$tool" -A MO_TS_IN -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  if [[ "$tool" == iptables ]]; then
    sudo "$tool" -A MO_TS_IN -s "$CONTROLLER_IP" -p tcp --dport 2222 -j ACCEPT
  fi
  sudo "$tool" -A MO_TS_IN -j DROP
  sudo "$tool" -I INPUT 1 -i tailscale0 -j MO_TS_IN
  sudo "$tool" -N MO_TS_OUT
  sudo "$tool" -A MO_TS_OUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  sudo "$tool" -A MO_TS_OUT -j REJECT
  sudo "$tool" -I OUTPUT 1 -o tailscale0 -j MO_TS_OUT
  sudo "$tool" -I FORWARD 1 -o tailscale0 -j DROP
done
# Candidate build scripts cannot reach private networks or cloud metadata.
sudo iptables -N MO_CONTAINER_OUT
sudo iptables -A MO_CONTAINER_OUT -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
for net in 10.0.0.0/8 100.64.0.0/10 169.254.0.0/16 172.16.0.0/12 192.168.0.0/16; do
  sudo iptables -A MO_CONTAINER_OUT -d "$net" -j REJECT
done
sudo iptables -A MO_CONTAINER_OUT -j RETURN
sudo iptables -I DOCKER-USER 1 -i docker0 -j MO_CONTAINER_OUT
