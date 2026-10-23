"""Streamlit 看板：运行总览 / 异常处置 / 运营复盘。"""
from __future__ import annotations

import os

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
import streamlit.components.v1 as components

API_BASE = os.environ.get("APRONOPS_API_BASE", "http://localhost:8000")

st.set_page_config(page_title="ApronOps", layout="wide")


def api_get(path: str, **params):
    r = requests.get(f"{API_BASE}{path}", params=params, timeout=5)
    r.raise_for_status()
    return r.json()


def api_post(path: str, json=None, **params):
    r = requests.post(f"{API_BASE}{path}", json=json, params=params, timeout=5)
    r.raise_for_status()
    return r.json()


@st.cache_data
def get_layout_cached():
    # 机坪拓扑运行期不变，缓存以省一次往返
    return api_get("/layout")


# 以下色值取自 dataviz 技能的默认调色板（light 模式）
HIGHLIGHT_COLORS = ["#2a78d6", "#eb6834", "#1baf7a"]  # 分类色槎位 1-3，仅供用户主动高亮的最多 3 辆车使用
EDGE_COLOR = "#e1e0d9"
NODE_FILL_COLOR = "#c3c2b7"
INK_PRIMARY = "#0b0b0b"
TRACK_COLOR = "#898781"
VEHICLE_MARKER_COLOR = "#52514e"
ANOMALOUS_STATES = {"DEGRADED", "PAUSED", "SAFE_STOP", "REMOTE_ASSIST"}


def build_topology_figure(layout_data, vehicles, tracks, highlight_ids):
    """机坪拓扑（作业点+连接边）叠加车辆轨迹与当前位置。

    车辆身份靠文字标签区分，不靠颜色——同一坐标系里任意两辆车的轨迹都可能
    相邻/交叉，调色板在这种"全两两相邻"场景下只有前 3 槎位能过色觉校验，
    因此默认全部车辆用统一灰色尾迹，仅用户主动勾选（最多 3 辆）时才换成
    分类色高亮。
    """
    fig = go.Figure()

    nodes = layout_data["nodes"]
    node_by_id = {n["id"]: n for n in nodes}
    seen_edges = set()
    for e in layout_data["edges"]:
        key = frozenset((e["from"], e["to"]))
        if key in seen_edges:
            continue
        seen_edges.add(key)
        a, b = node_by_id[e["from"]], node_by_id[e["to"]]
        fig.add_trace(go.Scatter(
            x=[a["x"], b["x"]], y=[a["y"], b["y"]],
            mode="lines", line=dict(color=EDGE_COLOR, width=2),
            hoverinfo="skip", showlegend=False,
        ))

    styles = {
        "stand": ("机位", "square", 11, "#2a78d6"),
        "stand_front": ("机位连接点", "circle", 4, "#b4bec7"),
        "taxiway": ("滑行节点", "circle", 5, "#8795a2"),
        "entry": ("入口", "triangle-up", 12, "#1baf7a"),
        "exit": ("出口", "triangle-down", 12, "#eb6834"),
    }
    for kind, (name, symbol, size, color) in styles.items():
        group = [n for n in nodes if n["type"] == kind]
        if not group:
            continue
        labelled = kind in ("stand", "entry", "exit")
        fig.add_trace(go.Scatter(
            x=[n["x"] for n in group], y=[n["y"] for n in group],
            mode="markers+text" if labelled else "markers",
            marker=dict(symbol=symbol, size=size, color=color),
            text=[n["label"] for n in group],
            customdata=[n["id"] for n in group],
            textposition="top center", textfont=dict(size=10, color=INK_PRIMARY),
            hovertemplate="%{text}<br>节点 ID: %{customdata}<extra></extra>",
            name=name, showlegend=True,
        ))

    highlight_color = {vid: HIGHLIGHT_COLORS[i] for i, vid in enumerate(highlight_ids[:3])}

    for v in vehicles:
        vid = v["id"]
        points = tracks.get(vid, [])
        is_highlighted = vid in highlight_color
        color = highlight_color.get(vid, TRACK_COLOR)
        xs, ys = [p["x"] for p in points], [p["y"] for p in points]
        if xs:
            fig.add_trace(go.Scatter(
                x=xs, y=ys, mode="lines",
                line=dict(color=color, width=2),
                opacity=1.0 if is_highlighted else 0.5,
                showlegend=is_highlighted,
                name=vid,
                hoverinfo="skip",
            ))

        is_anomalous = v["state"] in ANOMALOUS_STATES
        label = vid + ("  ⚠" if is_anomalous else "")
        fig.add_trace(go.Scatter(
            x=[v["x"]], y=[v["y"]],
            mode="markers+text",
            marker=dict(
                symbol="triangle-up" if is_anomalous else "circle",
                size=16 if is_anomalous else 12,
                color=color,
                line=dict(color="#ffffff", width=1),
            ),
            text=[label],
            textposition="bottom center",
            textfont=dict(color=INK_PRIMARY),
            hovertemplate=(
                f"车辆 {vid}<br>状态: {v['state']}<br>电量: {v['battery']:.0f}%"
                f"<br>实时速度: {v.get('current_speed_mps', 0.0):.1f} m/s"
                f"<br>当前任务: {v.get('current_task_id') or '无'}<extra></extra>"
            ),
            showlegend=False,
            name=vid,
        ))

    fig.update_layout(
        height=520,
        margin=dict(l=20, r=20, t=20, b=40),
        plot_bgcolor="#fcfcfb",
        paper_bgcolor="#fcfcfb",
        xaxis=dict(visible=False),
        yaxis=dict(visible=False, scaleanchor="x", scaleratio=1),
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    return fig


page = st.sidebar.radio("导航", ["运行总览", "异常处置", "运营复盘"])

if page == "运行总览":
    st.title("运行总览")
    try:
        vehicles = api_get("/vehicles")
        tasks = api_get("/tasks")
        alerts = api_get("/alerts")
        tracks = api_get("/tracks")
    except requests.RequestException as e:
        st.error(f"无法连接 API（{API_BASE}）：{e}")
        st.stop()

    col1, col2, col3 = st.columns(3)
    col1.metric("车辆总数", len(vehicles))
    col2.metric("活跃任务", sum(1 for t in tasks if t["status"] in ("ASSIGNED", "IN_PROGRESS")))
    col3.metric("未解决告警", sum(1 for a in alerts if not a.get("resolved_at")))

    ws_url = API_BASE.replace("http://", "ws://").replace("https://", "wss://") + "/ws/state"
    components.html(
        f"""
        <div id="ws-status" style="font-family: sans-serif; font-size: 14px; padding: 4px 0; color: #52514e;">
          ⚪ 正在连接实时推送…
        </div>
        <script>
          const el = document.getElementById("ws-status");
          const ws = new WebSocket("{ws_url}");
          ws.onmessage = (event) => {{
            const msg = JSON.parse(event.data);
            const now = new Date().toLocaleTimeString();
            el.textContent = "\\ud83d\\udfe2 实时连接 · 第 " + msg.tick + " 次推送 · 未解决告警 " + msg.unresolved_alerts + " · 最近更新 " + now;
          }};
          ws.onclose = () => {{ el.textContent = "\\ud83d\\udd34 实时连接已断开"; }};
          ws.onerror = () => {{ el.textContent = "\\ud83d\\udd34 实时连接错误"; }};
        </script>
        """,
        height=40,
    )

    st.subheader("Layout H 机坪拓扑与车辆轨迹")
    st.caption("30 个机位 · 103 个节点 · 119 条双向路段；支持缩放、拖动和悬浮查看。")
    vehicle_ids = sorted(v["id"] for v in vehicles)
    highlight_ids = st.multiselect(
        "高亮车辆轨迹（最多 3 辆，用颜色区分；未选中的车辆靠标签识别身份）",
        vehicle_ids, max_selections=3,
    )
    try:
        layout_data = get_layout_cached()
        st.plotly_chart(
            build_topology_figure(layout_data, vehicles, tracks, highlight_ids),
            use_container_width=True,
        )
    except requests.RequestException as e:
        st.error(f"无法加载机坪拓扑：{e}")
        st.stop()

    st.subheader("车辆状态表")
    if vehicles:
        vdf = pd.DataFrame(vehicles)[["id", "type", "state", "x", "y", "current_speed_mps", "battery", "current_task_id"]]
        vdf = vdf.rename(columns={"current_speed_mps": "实时速度(m/s)"})
        st.dataframe(vdf, use_container_width=True)

    st.subheader("任务列表")
    if tasks:
        tdf = pd.DataFrame(tasks)[["id", "type", "status", "priority", "origin", "destination", "assigned_vehicle_id"]]
        st.dataframe(tdf, use_container_width=True)

    with st.form("create_task"):
        st.write("手动创建任务")
        c1, c2, c3, c4 = st.columns(4)
        task_id = c1.text_input("任务 ID", value=f"TASK_{len(tasks) + 1:03d}")
        task_type = c2.selectbox("类型", ["baggage_tow", "catering_supply", "inspection"])
        node_options = [n["id"] for n in layout_data["nodes"]]
        node_labels = {n["id"]: f'{n["label"]}（{n["id"]}）' for n in layout_data["nodes"]}
        origin = c3.selectbox("起点", node_options, index=node_options.index("0"), format_func=node_labels.get)
        destination = c4.selectbox("终点", node_options, index=node_options.index("900"), format_func=node_labels.get)
        priority = st.slider("优先级", 1, 5, 3)
        submitted = st.form_submit_button("创建")
        if submitted:
            try:
                api_post("/tasks", json={
                    "id": task_id, "type": task_type, "origin": origin,
                    "destination": destination, "priority": priority,
                })
                st.success(f"任务 {task_id} 已创建")
            except requests.RequestException as e:
                st.error(f"创建失败：{e}")

elif page == "异常处置":
    st.title("异常处置")
    try:
        alerts = api_get("/alerts")
        vehicles = api_get("/vehicles")
    except requests.RequestException as e:
        st.error(f"无法连接 API（{API_BASE}）：{e}")
        st.stop()

    st.subheader("手动注入异常（演示用）")
    c1, c2, c3 = st.columns(3)
    vehicle_id = c1.selectbox("车辆", [v["id"] for v in vehicles])
    anomaly_type = c2.selectbox("异常类型", ["communication_lost", "position_quality_degraded", "route_blocked"])
    if c3.button("注入"):
        try:
            api_post("/anomalies/inject", anomaly_type=anomaly_type, vehicle_id=vehicle_id)
            st.success("已注入，等待下一次模拟推进后生效")
        except requests.RequestException as e:
            st.error(f"注入失败：{e}")

    st.subheader("告警列表")
    if alerts:
        adf = pd.DataFrame(alerts)
        st.dataframe(adf, use_container_width=True)
        unresolved = [a for a in alerts if not a.get("resolved_at")]
        if unresolved:
            alert_id = st.selectbox("选择要处置的告警", [a["id"] for a in unresolved])
            resolution = st.text_input("处置说明", value="人工确认恢复")
            if st.button("标记已处置"):
                try:
                    api_post(f"/alerts/{alert_id}/resolve", json={"resolution": resolution})
                    st.success("已处置")
                except requests.RequestException as e:
                    st.error(f"处置失败：{e}")
    else:
        st.info("暂无告警")

else:
    st.title("运营复盘")
    try:
        m = api_get("/metrics")
    except requests.RequestException as e:
        st.error(f"无法连接 API（{API_BASE}）：{e}")
        st.stop()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("任务完成率", f"{m['completion_rate'] * 100:.1f}%")
    c2.metric("准点率", f"{m['on_time_rate'] * 100:.1f}%")
    c3.metric("人工介入率", f"{m['manual_intervention_rate'] * 100:.1f}%")
    c4.metric("平均完成时长(s)", f"{m['avg_completion_time_s']:.0f}")

    st.subheader("原始指标")
    st.json(m)
