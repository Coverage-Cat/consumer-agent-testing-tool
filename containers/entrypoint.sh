#!/bin/sh
set -eu
iptables -P OUTPUT DROP
iptables -P INPUT DROP
iptables -P FORWARD DROP
ip6tables -P OUTPUT DROP
ip6tables -P INPUT DROP
ip6tables -P FORWARD DROP
iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
if [ "$1" = target ] || [ "$1" = target-gate ]; then
    iptables -A INPUT -p tcp --dport "${SERVICE_PORT:-8765}" -j ACCEPT
    iptables -A INPUT -p tcp --dport 8766 -j ACCEPT
    # Coordinator-generated IPv4:port pairs only. The evaluated CLI has no
    # NET_ADMIN and never receives these target-side exceptions.
    for destination in ${CONNECTOR_EGRESS:-}; do
        address=${destination%:*}
        port=${destination##*:}
        iptables -A OUTPUT -d "$address" -p tcp --dport "$port" -j ACCEPT
    done
    mode=serve
    if [ "$1" = target-gate ]; then mode=canary; fi
    exec setpriv --bounding-set=-all --inh-caps=-all --ambient-caps=-all \
        python -m consumer_agent_testing_tool.fixture_service "$mode" --run-id "$2" --variant "$3"
fi
iptables -A OUTPUT -d "$FIXTURE_IP" -p tcp --dport "${SERVICE_PORT:-8765}" -j ACCEPT
touch /tmp/boundary-ready
exec setpriv --reuid=1000 --regid=1000 --clear-groups --bounding-set=-all sleep infinity
