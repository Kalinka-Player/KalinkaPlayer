"""Capture the pre-port Python protocol. Pass its src directory as argv[1]."""
import json
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from kalinka_provision import protocol as p
from kalinka_provision.machine import Provisioner

commands = [
    {'v': 1, 'op': 'join', 'ssid': 'Café & home', 'password': 'dummy-passphrase', 'country': 'GB'},
    {'v': 1, 'op': 'scan', 'country': 'GB'},
    {'v': 1, 'op': 'networks', 'page': 9},
    {'v': 1, 'op': 'complete'}, {'v': 1, 'op': 'change_network'},
    {'v': True, 'op': 'complete'}, {'v': 1.0, 'op': 'complete'},
    {'v': 1, 'op': 'networks', 'page': True}, {'v': 1, 'op': 'networks', 'page': 10},
    {'v': 1, 'op': 'scan', 'country': 'gb'}, None, [],
]
for key, value in [('ssid', 'é' * 17), ('ssid', 'bad\nssid'), ('password', 'short'), ('password', 'é' * 10), ('country', None)]:
    commands.append(dict(commands[0], **{key: value}))
result = {'commands': [], 'statuses': [], 'pages': []}
for command in commands:
    raw = json.dumps(command, ensure_ascii=False, separators=(',', ':'))
    try:
        p.decode_command(raw.encode())
        valid = True
    except p.ProvisionError:
        valid = False
    result['commands'].append({'raw': raw, 'valid': valid})
for test in [False, True]:
    for state in p.STATES:
        for reason in p.REASONS:
            result['statuses'].append({'state': p.STATES.index(state), 'reason': p.REASONS.index(reason), 'test': test, 'hex': p.status_bytes(state, reason, '192.0.2.5', 8000, test, True, True, True).hex()})
m = Provisioner(None, identity_file='/does-not-exist')
result['pages'].append(m.networks().decode())
m.scan_id, m.scan_state = 1, 'ready'
m.scan_results = [{'ssid': s, 'signal': -42, 'security': 'wpa2'} for s in ['Café & home', '"' * 32, '\\' * 32, 'fourth']]
result['pages'].append(m.networks().decode())
m.scan_page = 1
result['pages'].append(m.networks().decode())
Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
