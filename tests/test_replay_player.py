"""Portable player exports preserve replay data and isolate uploaded strings."""
import json
from html.parser import HTMLParser

from apronops.dashboard.replay_player import build_replay_html


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.current = dict(attrs)
            self.current['text'] = ''
            self.scripts.append(self.current)

    def handle_data(self, data):
        if self.current is not None:
            self.current['text'] += data

    def handle_endtag(self, tag):
        if tag == 'script':
            self.current = None


def test_export_keeps_labels_as_data_and_has_no_network_dependencies():
    malicious = '</script><script>alert("not executable")</script>&车辆'
    data = dict(manifest={'scenario_id': malicious}, layout={'nodes': []},
                frames=[{'elapsed_s': 0, 'vehicles': [{'id': malicious}]}], summary={})
    html = build_replay_html(data)
    parsed = Scripts()
    parsed.feed(html)
    assert len(parsed.scripts) == 2
    assert all('src' not in script for script in parsed.scripts)
    assert json.loads(parsed.scripts[0]['text']) == data
    assert malicious not in html
