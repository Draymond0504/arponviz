"""Exercise the real Streamlit forms against an isolated API and database."""
import json
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from apronops.api import main

APP = str(Path(__file__).resolve().parents[1] / 'apronops/dashboard/app.py')


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('APRONOPS_DB_PATH', str(tmp_path / 'ui.db'))
    monkeypatch.setattr(main, 'TICK_INTERVAL_S', 3600.)
    with TestClient(main.app) as client:
        def adapter(method):
            def call(url, **kwargs):
                from urllib.parse import urlsplit
                kwargs.pop('timeout', None)
                result = client.request(method, urlsplit(url).path, **kwargs)
                response = requests.Response()
                response.status_code = result.status_code
                response._content = result.content
                response.url = url
                return response
            return call
        monkeypatch.setattr(requests, 'get', adapter('GET'))
        monkeypatch.setattr(requests, 'post', adapter('POST'))
        yield client


def assert_clean(app):
    assert not app.exception, [e.message for e in app.exception]
    assert not app.error, [e.value for e in app.error]


def button(app, label):
    return next(b for b in app.button if b.label == label)


def test_overview_create_task_and_refresh_preserves_form(client):
    app = AppTest.from_file(APP, default_timeout=20).run()
    assert_clean(app)
    task_id = next(t for t in app.text_input if t.label == '任务 ID')
    task_id.set_value('UI_TASK').run()
    button(app, '创建').click().run()
    assert_clean(app)
    assert client.get('/tasks').json()[-1]['id'] == 'UI_TASK'
    assert next(t for t in app.text_input if t.label == '任务 ID').value == 'UI_TASK'
    app.checkbox[0].check().run()
    app.checkbox[1].check().run()
    assert_clean(app)
    state = client.get('/state').json()
    assert len(state['tasks']) == 5
    app.sidebar.radio[0].set_value('运营复盘').run()
    assert_clean(app)
    assert any(m.label == '到期任务准时率' for m in app.metric)


def test_road_closure_and_resolution_from_forms(client):
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.sidebar.radio[0].set_value('异常处置').run()
    button(app, '封闭道路').click().run()
    assert_clean(app)
    closure = client.get('/roads/closures').json()[0]
    assert closure['reopened_at'] is None
    # Next refresh presents the newly created alert without leaving the page.
    app.run()
    button(app, '确认恢复并关闭告警').click().run()
    assert_clean(app)
    assert client.get('/roads/closures').json()[0]['reopened_at'] is not None
    assert not any(e['closed'] for e in client.get('/layout').json()['edges'])


def test_no_route_is_visible_without_hiding_overview(client):
    edges = client.get('/layout').json()['edges']
    edge = next(e for e in edges if e['to'] == 'SP_20')
    client.post('/roads/closures', json={'from_node': edge['from'], 'to_node': edge['to'], 'reason': '测试封闭'})
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.checkbox[1].check().run()
    assert_clean(app)
    assert any('暂无可通行路线' in w.value for w in app.warning)
    assert len(app.dataframe) >= 2
    # Inspect Plotly data, including the real closure rendering (not just text).
    chart = json.loads(app.get('plotly_chart')[0].proto.spec)
    assert any(t.get('name') == '封闭路段' for t in chart['data'])
    assert not any(t.get('name') == '预览路线' for t in chart['data'])


def test_flight_page_demo_delay_and_crossing_request(client):
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.sidebar.radio[0].set_value('航班与穿越').run()
    assert_clean(app)
    button(app, '载入航班与穿越演示').click().run()
    assert_clean(app)
    assert len(client.get('/flights').json()) == 1
    button(app, '提交航班延误').click().run()
    assert_clean(app)
    assert client.get('/flights').json()[0]['revision'] == 2
    button(app, '保存穿越请求').click().run()
    assert_clean(app)
    assert len(client.get('/crossings/occupancies').json()) == 3
    button(app, '创建航班并生成保障任务').click().run()
    assert_clean(app)
    assert len(client.get('/flights').json()) == 2
    assert len([t for t in client.get('/tasks').json() if t['flight_id']]) == 6


def test_experiment_replay_scrub_without_live_api(tmp_path, monkeypatch):
    from apronops.experiments.run import run_one
    from apronops.experiments.scenario import Scenario
    dest = tmp_path/'batch/run'
    run_one(Scenario(id='ui', horizon_s=3, sample_s=.5, closures=[
        dict(id='closure', start_s=1, end_s=2, from_node='DEPOT_AMR', to_node='R_009')]),
        1, 'priority_distance', dest)
    monkeypatch.setenv('APRONOPS_RESULTS_DIR', str(tmp_path))
    calls = []
    def offline(*args, **kwargs):
        calls.append(args)
        raise requests.ConnectionError('offline')
    monkeypatch.setattr(requests, 'get', offline)
    monkeypatch.setattr(requests, 'post', offline)
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.sidebar.radio[0].set_value('实验回放').run()
    calls.clear()
    app.selectbox[0].select(dest/'replay.json').run()
    assert_clean(app)
    assert len(app.get('plotly_chart')) == 1
    app.slider[0].set_value(2).run()
    assert_clean(app)
    chart = json.loads(app.get('plotly_chart')[0].proto.spec)
    assert any(t.get('name') == '封闭路段' for t in chart['data'])
    app.slider[0].set_value(6).run()
    assert_clean(app)
    chart = json.loads(app.get('plotly_chart')[0].proto.spec)
    assert not any(t.get('name') == '封闭路段' for t in chart['data'])
    assert not calls
    (dest/'replay.json').write_text('{"bad": true}')
    app.run()
    assert not app.exception
    assert app.error


def test_policy_form_and_depot_capacity_display(client):
    app=AppTest.from_file(APP,default_timeout=20).run()
    assert_clean(app)
    assert any(h.value=='基地泊位与入口排队' for h in app.subheader)
    selector=next(s for s in app.selectbox if s.label=='规则策略')
    selector.select('rolling_optimization').run()
    button(app,'应用调度策略').click().run()
    assert_clean(app)
    assert client.get('/state').json()['policy']=='rolling_optimization'
    assert client.get('/dispatch/policies').json()['current']=='rolling_optimization'
    assert client.post('/dispatch/policy',params={'policy':'invalid'}).status_code==400
    assert len(client.get('/state').json()['depots'])==5
