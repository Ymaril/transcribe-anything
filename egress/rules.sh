#!/bin/sh
# Egress filter for the network namespace shared by a sidecar and the sandboxed container.
# usage: rules.sh QUOTA_BYTES
# Public internet is open; private ranges (host, LAN, other containers) are not.
# Outbound bytes are capped: downloads are cheap (~0.5% of their size in ACKs), uploads are not.
set -e
iptables -A OUTPUT -o lo -j ACCEPT
for ns in $(awk '/^nameserver/{print $2}' /etc/resolv.conf); do
  iptables -A OUTPUT -d "$ns" -p udp --dport 53 -j ACCEPT
  iptables -A OUTPUT -d "$ns" -p tcp --dport 53 -j ACCEPT
done
for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10; do
  iptables -A OUTPUT -d "$net" -j DROP
done
iptables -A OUTPUT -m quota --quota "$1" -j ACCEPT
iptables -A OUTPUT -j DROP
