"""Read-only comparison of completed paired study runs."""
import json
import os
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

LABELS={
    'completion_rate':'任务完成率', 'due_on_time_rate':'到期任务准时率',
    'unfinished_tasks':'未完成任务数','tardiness_lower_bound_s':'累计任务延误下界（秒）',
    'aircraft_departed':'成功离场航空器数','aircraft_departure_delay_lower_bound_s':'航空器离位延误下界（秒）',
    'route_wait_s':'车辆路径等待（车辆秒）','depot_wait_s':'基地排队（车辆秒）',
    'deadlocks_unresolved':'未解除持续阻塞','distance_m':'车辆总里程（米）',
    'empty_distance_m':'空驶里程（米）','aircraft_wait_s':'航空器累计等待（秒）',
    'conflict_zone_violation_steps':'交叉区违规子步','depot_capacity_violation_steps':'泊位违规子步'}


def render():
    st.title('空地协同实验对比')
    st.caption('同一条件与种子的三种策略使用相同输入；只汇总策略齐全且输入哈希一致的配对。')
    root=Path(os.environ.get('APRONOPS_RESULTS_DIR',str(Path(__file__).resolve().parents[2]/'results/experiments')))
    files=sorted(p for p in root.glob('*/study-report.json') if p.resolve().is_relative_to(root.resolve()))
    if not files:
        st.info('尚无对照实验。按 docs/aircraft-and-studies.md 运行 study 命令后，在这里查看。');return
    selected=st.selectbox('对照实验批次',files,index=len(files)-1,format_func=lambda p:p.parent.name)
    try:
        report=json.loads(selected.read_text(encoding='utf-8'))
        groups=report['groups'];runs=report['runs']
        a,b,c=st.columns(3)
        a.metric('已完成运行 / 计划运行',f"{report['completed_runs']} / {report['planned_runs']}")
        b.metric('失败或中断',report['failed_runs']+report['interrupted_runs'])
        c.metric('未纳入统计的条件种子配对',len(report['excluded_pairs']))
        if report['excluded_pairs']:
            st.warning('存在尚未完成或无法配对的输入，相关条件种子未进入下方均值。')
        metric=st.selectbox('对比指标',list(LABELS),format_func=LABELS.get)
        mode=st.radio('统计视角',['策略原始值','相对最近车辆策略的配对差值'],horizontal=True)
        data=groups if mode=='策略原始值' else report['paired_deltas']
        scale=100 if metric.endswith('_rate') else 1
        fig=go.Figure()
        for policy,color in zip(dict.fromkeys(g['policy'] for g in data),['#2979c8','#de7c39','#238b6f']):
            points=[g for g in data if g['policy']==policy]
            fig.add_bar(name=policy,x=[g['condition'] for g in points],
                y=[g[metric+'_mean']*scale if g[metric+'_mean'] is not None else None for g in points],
                error_y=dict(type='data',array=[g[metric+'_sd']*scale if g[metric+'_sd'] is not None else 0 for g in points]),
                customdata=[[g['label'],g[metric+'_n']] for g in points],marker_color=color,
                hovertemplate='%{customdata[0]}<br>值 %{y:.2f}<br>样本数 %{customdata[1]}<extra>%{fullData.name}</extra>')
        condition_labels={g['condition']:g['label'].split('：')[0] for g in data}
        fig.update_layout(barmode='group',height=410,yaxis_title=LABELS[metric]+('（% / 百分点）' if scale==100 else ''),
                          xaxis=dict(tickmode='array',tickvals=list(condition_labels),ticktext=list(condition_labels.values())),
                          legend=dict(orientation='h',y=-.2),margin=dict(t=25,b=80))
        st.plotly_chart(fig,use_container_width=True)
        st.caption('柱为种子均值，误差线为样本标准差，不是置信区间。差值 = 当前策略 − 最近车辆策略；完成率和离场数越高越好，延误和等待越低越好。')
        st.dataframe(pd.DataFrame([{k:g[k] for k in ('condition','label','policy','paired_seeds',metric+'_mean',metric+'_sd',metric+'_min',metric+'_max')} for g in data]),use_container_width=True)
        st.caption('航空器累计等待含机位保障等待；离位延误单独相对计划离位计算。未离位航空器和未完成任务保留在延误下界中。各条件航班数不同，离场数量需结合原始运行的 aircraft_total 阅读。')
        st.subheader('单次运行与复核')
        st.dataframe(pd.DataFrame(runs).reindex(columns=['condition','seed','policy','status','completed_tasks','released_tasks','aircraft_departed','aircraft_total','unfinished_tasks','deadlocks_unresolved','input_sha256','error']),use_container_width=True)
        available=[r for r in runs if r.get('run_dir') and (selected.parent/r['run_dir']/'replay.json').resolve().is_relative_to(selected.parent.resolve()) and (selected.parent/r['run_dir']/'replay.json').exists()]
        if available:
            item=st.selectbox('选择一次运行下载回放',available,format_func=lambda r:r['run_dir'])
            replay=selected.parent/item['run_dir']/'replay.json'
            st.download_button('下载该次 replay.json',replay.read_bytes(),file_name=item['run_dir']+'-replay.json',mime='application/json')
            st.caption('在“实验回放”选择同名运行即可完整播放；切换策略或种子不会改动在线现场。')
        st.download_button('下载配对统计 CSV',(selected.parent/'study-summary.csv').read_bytes(),file_name='study-summary.csv',mime='text/csv')
    except (ValueError,OSError,KeyError,TypeError) as error:
        st.error('无法读取实验报告：'+str(error))
