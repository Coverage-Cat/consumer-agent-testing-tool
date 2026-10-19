#!/bin/sh
set -eu
umask 077
# No browser process starts until BOTH address families are default-denied.
# The root driver alone can use the private loopback TCP transport.
for firewall in iptables ip6tables; do
    "$firewall" -w -P INPUT DROP
    "$firewall" -w -P OUTPUT DROP
    "$firewall" -w -P FORWARD DROP
    "$firewall" -w -A OUTPUT -o lo -m owner --uid-owner 0 -j ACCEPT
    "$firewall" -w -A INPUT -i lo -j ACCEPT
done
mkdir -p /control /tmp/browser-home /tmp/browser-profile /tmp/browser-downloads
chmod 0700 /control /tmp/browser-home /tmp/browser-profile /tmp/browser-downloads
chown 65532:65534 /tmp/browser-home /tmp/browser-profile /tmp/browser-downloads
# Root cannot change the firewall after startup. Only the identity-switching
# launcher retains SETUID/SETGID/SETPCAP; Chromium drops the entire bounding set.
exec setpriv --bounding-set=-all,+setuid,+setgid,+setpcap,+dac_read_search \
    --inh-caps=-all --ambient-caps=-all --no-new-privs \
    node /opt/worker/server.mjs
